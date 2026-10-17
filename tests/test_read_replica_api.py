"""
Replica-routed endpoints. The replica alias is a test mirror of default (a separate connection to
the same database), so these tests commit their data (transaction=True) for the replica to see it.
"""
import datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.db import connections
from django.utils import timezone
from rest_framework.test import APIClient

from ledger.models import Account, Tenant
from ledger.services import replica

private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
private_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
public_pem = private_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)

routed = pytest.mark.django_db(transaction=True, databases=['default', 'replica'])


@pytest.fixture(autouse=True)
def jwt_and_breaker(settings, monkeypatch):
    settings.JWT_PUBLIC_KEY = public_pem
    settings.READ_REPLICA_ENABLED = True
    monkeypatch.setattr(replica, "_unavailable_until", 0.0)


def headers_for(tenant):
    token = jwt.encode(
        {"tenant_id": str(tenant.id), "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)},
        private_pem,
        algorithm="RS256",
    )
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def tenant():
    return Tenant.objects.create(name="Replica Tenant")


@pytest.fixture
def bank(client, tenant):
    bank = Account.objects.create(tenant=tenant, name="Bank", type=Account.Type.ASSET, currency="USD", allow_negative=True)
    sales = Account.objects.create(tenant=tenant, name="Sales", type=Account.Type.REVENUE, currency="USD")
    for i in range(2):
        resp = client.post('/api/v1/journal-entries/', format='json', HTTP_IDEMPOTENCY_KEY=f"replica-{i}", data={
            "reference_id": f"REPLICA-{i}",
            "lines": [
                {"account_id": str(bank.id), "amount": "10.00", "direction": "DEBIT"},
                {"account_id": str(sales.id), "amount": "10.00", "direction": "CREDIT"},
            ],
        }, **headers_for(tenant))
        assert resp.status_code == 201
    return bank


def routed_urls(account):
    as_of = timezone.now().isoformat().replace('+00:00', 'Z')
    return [
        f"/api/v1/accounts/{account.id}/statement/",
        f"/api/v1/accounts/{account.id}/balance/",
        f"/api/v1/accounts/{account.id}/balance/?as_of={as_of}",
    ]


@routed
def test_replica_serves_same_body_as_primary(client, tenant, bank, settings):
    auth = headers_for(tenant)
    urls = routed_urls(bank)

    settings.READ_REPLICA_ENABLED = False
    connections['replica'].close()
    primary = [client.get(url, **auth) for url in urls]
    assert [r['Read-Source'] for r in primary] == ['primary'] * 3
    assert connections['replica'].connection is None  # never contacted while disabled

    settings.READ_REPLICA_ENABLED = True
    served = [client.get(url, **auth) for url in urls]
    assert [r['Read-Source'] for r in served] == ['replica'] * 3
    assert [r.status_code for r in served] == [200] * 3
    assert [r.data for r in served] == [r.data for r in primary]
    assert len(served[0].data['results']) == 2


@routed
def test_replica_reads_are_tenant_scoped_under_runtime_role(client, tenant, bank):
    other = Tenant.objects.create(name="Other Replica Tenant")
    for alias in ('default', 'replica'):
        with connections[alias].cursor() as c:
            c.execute("SET ROLE ledger_runtime")
    try:
        resp = client.get(f"/api/v1/accounts/{bank.id}/statement/", **headers_for(tenant))
        assert resp['Read-Source'] == 'replica'
        assert len(resp.data['results']) == 2  # RLS saw the tenant context on the replica

        resp = client.get(f"/api/v1/accounts/{bank.id}/statement/", **headers_for(other))
        assert resp.status_code == 404
    finally:
        for alias in ('default', 'replica'):
            with connections[alias].cursor() as c:
                c.execute("RESET ROLE")


@routed
def test_strong_consistency_skips_lagging_replica(client, tenant, bank, monkeypatch):
    monkeypatch.setattr(replica, "replica_status", lambda start_lsn: (False, 1.0))
    auth = headers_for(tenant)
    url = f"/api/v1/accounts/{bank.id}/statement/"
    assert client.get(url, **auth)['Read-Source'] == 'replica'
    assert client.get(url + "?consistency=bounded", **auth)['Read-Source'] == 'replica'
    assert client.get(url + "?consistency=strong", **auth)['Read-Source'] == 'primary'


@routed
def test_unreachable_replica_serves_from_primary(client, tenant, bank, monkeypatch):
    from django.db import OperationalError

    def down(start_lsn):
        raise OperationalError("could not connect to server")
    monkeypatch.setattr(replica, "replica_status", down)
    resp = client.get(f"/api/v1/accounts/{bank.id}/balance/", **headers_for(tenant))
    assert resp.status_code == 200
    assert resp['Read-Source'] == 'primary'


@routed
def test_unknown_consistency_is_400(client, tenant, bank):
    resp = client.get(f"/api/v1/accounts/{bank.id}/balance/?consistency=eventual", **headers_for(tenant))
    assert resp.status_code == 400
    assert resp.data['error']['code'] == 'invalid'


@routed
def test_account_detail_is_not_routed(client, tenant, bank):
    resp = client.get(f"/api/v1/accounts/{bank.id}/", **headers_for(tenant))
    assert resp.status_code == 200
    assert 'Read-Source' not in resp
