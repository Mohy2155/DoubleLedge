import datetime
import uuid
from decimal import Decimal
import jwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from rest_framework.test import APIClient

from ledger.models import Account, Tenant, Hold
from ledger.services.sharding import create_sharded_account

private_key = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
    backend=default_backend()
)
public_pem = private_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo
)
private_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)

@pytest.fixture(autouse=True)
def override_jwt_settings(settings):
    settings.JWT_PUBLIC_KEY = public_pem

@pytest.fixture
def api_client():
    return APIClient()

def create_auth_headers(tenant):
    payload = {
        "tenant_id": str(tenant.id),
        "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=3600)
    }
    token = jwt.encode(payload, private_pem, algorithm="RS256")
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

@pytest.mark.django_db
def test_demo_ui_endpoint_returns_200(api_client):
    res = api_client.get('/demo/')
    assert res.status_code == 200
    assert "DoubleLedge Demo UI" in res.content.decode('utf-8')

@pytest.mark.django_db
def test_accounts_list_tenant_scoped_and_filters_shards(api_client):
    tenant1 = Tenant.objects.create(name="Tenant 1")
    tenant2 = Tenant.objects.create(name="Tenant 2")

    # Regular top-level account for Tenant 1
    t1_acc = Account.objects.create(
        tenant=tenant1,
        name="T1 Regular",
        type=Account.Type.ASSET,
        currency="USD",
        balance=Decimal("100.0000")
    )

    # Sharded account (master + shards) for Tenant 1
    t1_master = create_sharded_account(
        tenant_id=tenant1.id,
        name="T1 Sharded",
        account_type="ASSET",
        currency="USD",
        allow_negative=True,
        shard_count=3
    )

    # Top-level account for Tenant 2
    t2_acc = Account.objects.create(
        tenant=tenant2,
        name="T2 Secret",
        type=Account.Type.ASSET,
        currency="USD",
        balance=Decimal("500.0000")
    )

    headers1 = create_auth_headers(tenant1)
    res1 = api_client.get('/api/v1/accounts/', **headers1)
    assert res1.status_code == 200

    data1 = res1.json()
    account_ids = [a['id'] for a in data1]

    # Must contain top-level accounts for tenant 1
    assert str(t1_acc.id) in account_ids
    assert str(t1_master.id) in account_ids

    # Must NOT contain tenant 2's accounts
    assert str(t2_acc.id) not in account_ids

    # Must NOT contain any shard account ids (parent_account_id is not null)
    shard_ids = list(Account.objects.filter(parent_account_id=t1_master.id).values_list('id', flat=True))
    assert len(shard_ids) == 3
    for s_id in shard_ids:
        assert str(s_id) not in account_ids

    # Tenant 2 check
    headers2 = create_auth_headers(tenant2)
    res2 = api_client.get('/api/v1/accounts/', **headers2)
    assert res2.status_code == 200
    data2 = res2.json()
    assert len(data2) == 1
    assert data2[0]['id'] == str(t2_acc.id)

@pytest.mark.django_db
def test_holds_list_tenant_scoped_and_status_filter(api_client):
    tenant1 = Tenant.objects.create(name="Tenant 1")
    tenant2 = Tenant.objects.create(name="Tenant 2")

    acc1 = Account.objects.create(tenant=tenant1, name="T1 Acc", type=Account.Type.ASSET, currency="USD")
    acc2 = Account.objects.create(tenant=tenant2, name="T2 Acc", type=Account.Type.ASSET, currency="USD")

    h1_pending = Hold.objects.create(
        tenant=tenant1,
        account=acc1,
        reference_id="h1",
        amount=Decimal("50.0000"),
        currency="USD",
        status=Hold.Status.PENDING,
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
    )
    h1_captured = Hold.objects.create(
        tenant=tenant1,
        account=acc1,
        reference_id="h2",
        amount=Decimal("20.0000"),
        currency="USD",
        status=Hold.Status.CAPTURED,
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
    )
    h2_hold = Hold.objects.create(
        tenant=tenant2,
        account=acc2,
        reference_id="h3",
        amount=Decimal("100.0000"),
        currency="USD",
        status=Hold.Status.PENDING,
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
    )

    headers1 = create_auth_headers(tenant1)

    # All holds for tenant1 (ordered newest first)
    res = api_client.get('/api/v1/holds/', **headers1)
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2
    ids = [h['id'] for h in data]
    assert str(h1_captured.id) in ids
    assert str(h1_pending.id) in ids
    assert str(h2_hold.id) not in ids

    # Filter by status=PENDING
    res_filtered = api_client.get('/api/v1/holds/?status=PENDING', **headers1)
    assert res_filtered.status_code == 200
    data_filtered = res_filtered.json()
    assert len(data_filtered) == 1
    assert data_filtered[0]['id'] == str(h1_pending.id)
