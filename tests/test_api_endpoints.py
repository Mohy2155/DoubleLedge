import datetime
import uuid
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from rest_framework.test import APIClient

from ledger.models import Account, IdempotencyKey, JournalEntry, Tenant, TransactionLine

private_key = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
    backend=default_backend()
)
public_key = private_key.public_key()
private_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)
public_pem = public_key.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo
)

@pytest.fixture(autouse=True)
def override_jwt_settings(settings):
    settings.JWT_PUBLIC_KEY = public_pem

@pytest.fixture
def api_client():
    return APIClient()

@pytest.fixture
def tenant():
    return Tenant.objects.create(name="Test Tenant")

@pytest.fixture
def auth_headers(tenant):
    payload = {
        "tenant_id": str(tenant.id),
        "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=3600)
    }
    token = jwt.encode(payload, private_pem, algorithm="RS256")
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

@pytest.fixture
def accounts(tenant):
    asset = Account.objects.create(tenant=tenant, name="Bank", type=Account.Type.ASSET, currency="USD")
    revenue = Account.objects.create(tenant=tenant, name="Sales", type=Account.Type.REVENUE, currency="USD")
    return asset, revenue

@pytest.mark.django_db
def test_create_journal_entry_and_idempotency(api_client, auth_headers, accounts):
    asset, revenue = accounts
    payload = {
        "reference_id": "tx_123",
        "description": "Sale",
        "lines": [
            {"account_id": str(asset.id), "amount": "100.0000", "direction": "DEBIT"},
            {"account_id": str(revenue.id), "amount": "100.0000", "direction": "CREDIT"}
        ]
    }
    
    response = api_client.post('/api/v1/journal-entries/', data=payload, format='json', HTTP_IDEMPOTENCY_KEY='idem_1', **auth_headers)
    assert response.status_code == 201
    response.data['id']
    
    response2 = api_client.post('/api/v1/journal-entries/', data=payload, format='json', HTTP_IDEMPOTENCY_KEY='idem_1', **auth_headers)
    assert response2.status_code == 201
    assert response2.headers.get('Idempotent-Replayed') == 'true'

    # Test missing idempotency key
    response3 = api_client.post('/api/v1/journal-entries/', data=payload, format='json', **auth_headers)
    assert response3.status_code == 400
    assert response3.data['error']['code'] == 'missing_idempotency_key'

    # Test payload conflict
    payload_conflict = {
        "reference_id": "tx_123_conflict",
        "lines": [
            {"account_id": str(asset.id), "amount": "200.0000", "direction": "DEBIT"},
            {"account_id": str(revenue.id), "amount": "200.0000", "direction": "CREDIT"}
        ]
    }
    response4 = api_client.post('/api/v1/journal-entries/', data=payload_conflict, format='json', HTTP_IDEMPOTENCY_KEY='idem_1', **auth_headers)
    assert response4.status_code == 422
    assert response4.data['error']['code'] == 'idempotency_key_reuse'

@pytest.mark.django_db
def test_reverse_entry(api_client, auth_headers, accounts):
    asset, revenue = accounts
    payload = {
        "reference_id": "tx_abc",
        "lines": [
            {"account_id": str(asset.id), "amount": "50.0000", "direction": "DEBIT"},
            {"account_id": str(revenue.id), "amount": "50.0000", "direction": "CREDIT"}
        ]
    }
    res = api_client.post('/api/v1/journal-entries/', data=payload, format='json', HTTP_IDEMPOTENCY_KEY='idem_rev1', **auth_headers)
    entry_id = res.data['id']
    
    rev_payload = {"reference_id": "tx_abc_rev"}
    res_rev = api_client.post(f'/api/v1/journal-entries/{entry_id}/reverse/', data=rev_payload, format='json', HTTP_IDEMPOTENCY_KEY='idem_rev2', **auth_headers)
    assert res_rev.status_code == 201
    
    res_rev_again = api_client.post(f'/api/v1/journal-entries/{entry_id}/reverse/', data=rev_payload, format='json', HTTP_IDEMPOTENCY_KEY='idem_rev3', **auth_headers)
    assert res_rev_again.status_code == 409
    assert res_rev_again.data['error']['code'] == 'already_reversed'

@pytest.mark.django_db
def test_entry_unbalanced_within_a_currency_is_rejected(api_client, auth_headers, tenant):
    usd = Account.objects.create(tenant=tenant, name="Bank USD", type=Account.Type.ASSET, currency="USD", allow_negative=True)
    eur = Account.objects.create(tenant=tenant, name="Bank EUR", type=Account.Type.ASSET, currency="EUR", allow_negative=True)
    payload = {
        "reference_id": "tx_mixed",
        "lines": [
            {"account_id": str(usd.id), "amount": "10.0000", "direction": "DEBIT"},
            {"account_id": str(eur.id), "amount": "10.0000", "direction": "CREDIT"}
        ]
    }
    res = api_client.post('/api/v1/journal-entries/', data=payload, format='json', HTTP_IDEMPOTENCY_KEY='idem_mixed', **auth_headers)
    assert res.status_code == 422
    assert res.data['error']['code'] == 'unbalanced_entry'
    assert not JournalEntry.objects.filter(tenant=tenant).exists()
    assert not TransactionLine.objects.filter(tenant=tenant).exists()
    assert not IdempotencyKey.objects.filter(tenant=tenant).exists()

@pytest.mark.django_db
def test_entry_balanced_in_each_currency_is_posted(api_client, auth_headers, tenant, accounts):
    asset, revenue = accounts
    eur_asset = Account.objects.create(tenant=tenant, name="Bank EUR", type=Account.Type.ASSET, currency="EUR")
    eur_revenue = Account.objects.create(tenant=tenant, name="Sales EUR", type=Account.Type.REVENUE, currency="EUR")
    payload = {
        "reference_id": "tx_two_ccy",
        "lines": [
            {"account_id": str(asset.id), "amount": "10.0000", "direction": "DEBIT"},
            {"account_id": str(revenue.id), "amount": "10.0000", "direction": "CREDIT"},
            {"account_id": str(eur_asset.id), "amount": "5.0000", "direction": "DEBIT"},
            {"account_id": str(eur_revenue.id), "amount": "5.0000", "direction": "CREDIT"}
        ]
    }
    res = api_client.post('/api/v1/journal-entries/', data=payload, format='json', HTTP_IDEMPOTENCY_KEY='idem_two_ccy', **auth_headers)
    assert res.status_code == 201
    assert len(res.data['lines']) == 4

@pytest.mark.django_db
def test_get_account_balance(api_client, auth_headers, accounts):
    asset, revenue = accounts
    payload = {
        "reference_id": "tx_bal",
        "lines": [
            {"account_id": str(asset.id), "amount": "50.0000", "direction": "DEBIT"},
            {"account_id": str(revenue.id), "amount": "50.0000", "direction": "CREDIT"}
        ]
    }
    api_client.post('/api/v1/journal-entries/', data=payload, format='json', HTTP_IDEMPOTENCY_KEY='idem_bal', **auth_headers)
    
    import urllib.parse
    now = datetime.datetime.now(datetime.UTC).isoformat()
    now_encoded = urllib.parse.quote(now)
    res = api_client.get(f'/api/v1/accounts/{asset.id}/balance/?as_of={now_encoded}', **auth_headers)
    assert res.status_code == 200
    assert res.data['balance'] == "50.0000"

@pytest.mark.django_db
def test_rate_limiting_redis_fail_open(api_client, auth_headers):
    # Mock redis client to raise error
    from redis.exceptions import RedisError
    with patch('api.throttling.redis_client.register_script', side_effect=RedisError("Redis down")):
        res = api_client.get(f'/api/v1/accounts/{uuid.uuid4()}/', **auth_headers)
        # Even with error, it shouldn't 500, but fail open (404 since acc doesn't exist, not 429 or 500)
        assert res.status_code in [404, 401]

@pytest.mark.django_db
def test_swagger_schema(api_client):
    res = api_client.get('/api/docs/')
    assert res.status_code == 200
