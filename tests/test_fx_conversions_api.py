import datetime
import uuid
from decimal import Decimal

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from ledger.models import Account, Tenant
from ledger.services.fx import ensure_trading_accounts, fx_trading_account_id
from ledger.services.periods import close_period

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

RESPONSE_KEYS = {
    "id", "journal_entry_id", "from_account_id", "to_account_id", "from_currency", "to_currency",
    "from_amount", "to_amount", "rate", "rate_source", "rate_as_of", "effective_at", "created_at", "lines",
}


@pytest.fixture(autouse=True)
def override_jwt_settings(settings):
    settings.JWT_PUBLIC_KEY = public_pem


@pytest.fixture
def api_client():
    return APIClient()


def headers_for(tenant):
    token = jwt.encode(
        {"tenant_id": str(tenant.id), "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)},
        private_pem,
        algorithm="RS256",
    )
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.fixture
def tenant():
    return Tenant.objects.create(name="FX API Tenant")


@pytest.fixture
def auth(tenant):
    return headers_for(tenant)


@pytest.fixture
def cash(tenant):
    usd = Account.objects.create(tenant=tenant, name="Cash", type=Account.Type.ASSET, currency="USD", balance=Decimal("1000"))
    eur = Account.objects.create(tenant=tenant, name="Cash EUR", type=Account.Type.ASSET, currency="EUR")
    return usd, eur


def body_for(from_account, to_account, ref="FX-1", **overrides):
    return {
        "reference_id": ref,
        "from_account_id": str(from_account.id),
        "to_account_id": str(to_account.id),
        "from_amount": "100.00",
        "rate": "0.9234",
        **overrides,
    }


def post_conversion(client, auth, body, key="k-fx-1"):
    return client.post('/api/v1/fx-conversions/', data=body, format='json', HTTP_IDEMPOTENCY_KEY=key, **auth)


@pytest.mark.django_db
def test_create_returns_full_shape_and_replays(api_client, auth, tenant, cash):
    usd, eur = cash
    resp = post_conversion(api_client, auth, body_for(usd, eur))
    assert resp.status_code == 201
    assert set(resp.data) == RESPONSE_KEYS
    assert (resp.data["from_account_id"], resp.data["to_account_id"]) == (str(usd.id), str(eur.id))
    assert (resp.data["from_currency"], resp.data["to_currency"]) == ("USD", "EUR")
    assert (resp.data["from_amount"], resp.data["to_amount"], resp.data["rate"]) == ("100.0000", "92.3400", "0.9234")
    assert [(line["amount"], line["currency"]) for line in resp.data["lines"]] == [
        ("100.0000", "USD"), ("100.0000", "USD"), ("92.3400", "EUR"), ("92.3400", "EUR"),
    ]
    assert all(set(line) == {"account_id", "direction", "amount", "currency"} for line in resp.data["lines"])

    replay = post_conversion(api_client, auth, body_for(usd, eur))
    assert replay.status_code == 201
    assert replay.headers.get("Idempotent-Replayed") == "true"
    assert replay.data == resp.data

    names = {a["name"] for a in api_client.get('/api/v1/accounts/', **auth).data}
    assert {"FX Trading USD", "FX Trading EUR"} <= names


@pytest.mark.django_db
def test_missing_idempotency_key(api_client, auth, cash):
    resp = api_client.post('/api/v1/fx-conversions/', data=body_for(*cash), format='json', **auth)
    assert resp.status_code == 400
    assert resp.data["error"]["code"] == "missing_idempotency_key"


@pytest.mark.django_db
def test_list_newest_first_and_retrieve_stamp(api_client, auth, cash):
    usd, eur = cash
    ids = []
    for i in range(3):
        body = body_for(usd, eur, ref=f"FX-L{i}", rate_source="ECB", rate_as_of="2026-10-16T16:00:00Z")
        ids.append(post_conversion(api_client, auth, body, key=f"k-l{i}").data["id"])

    listed = api_client.get('/api/v1/fx-conversions/', **auth)
    assert listed.status_code == 200
    assert [c["id"] for c in listed.data] == ids[::-1]
    assert all(c["rate_source"] == "ECB" for c in listed.data)

    resp = api_client.get(f'/api/v1/fx-conversions/{ids[0]}/', **auth)
    assert resp.status_code == 200
    assert resp.data == listed.data[2]
    assert (resp.data["rate"], resp.data["rate_source"], resp.data["rate_as_of"]) == ("0.9234", "ECB", "2026-10-16T16:00:00Z")


@pytest.mark.django_db
@pytest.mark.runtime_role
def test_other_tenants_conversion_and_account_are_not_found(api_client):
    tenant_a = Tenant.objects.create(name="FX Tenant A")
    tenant_b = Tenant.objects.create(name="FX Tenant B")
    with connection.cursor() as c:
        c.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_a.id)])
    a_usd = Account.objects.create(tenant=tenant_a, name="Cash", type=Account.Type.ASSET, currency="USD", allow_negative=True)
    a_eur = Account.objects.create(tenant=tenant_a, name="Cash EUR", type=Account.Type.ASSET, currency="EUR")
    with connection.cursor() as c:
        c.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_b.id)])
    b_usd = Account.objects.create(tenant=tenant_b, name="Cash", type=Account.Type.ASSET, currency="USD", allow_negative=True)

    created = post_conversion(api_client, headers_for(tenant_a), body_for(a_usd, a_eur), key="k-a")
    assert created.status_code == 201

    auth_b = headers_for(tenant_b)
    resp = api_client.get(f'/api/v1/fx-conversions/{created.data["id"]}/', **auth_b)
    assert resp.status_code == 404
    assert resp.data["error"]["code"] == "not_found"
    assert api_client.get('/api/v1/fx-conversions/', **auth_b).data == []

    resp = post_conversion(api_client, auth_b, body_for(b_usd, a_eur), key="k-b")
    assert resp.status_code == 404
    assert resp.data["error"]["code"] == "not_found"


def overrides_for(code, tenant):
    """Request-body changes (plus any setup) that make a conversion fail with `code`."""
    if code == "same_currency":
        bank = Account.objects.create(tenant=tenant, name="Bank", type=Account.Type.ASSET, currency="USD")
        return {"to_account_id": str(bank.id)}
    if code == "invalid_account":
        ensure_trading_accounts(tenant.id, ["USD", "EUR"])
        return {"to_account_id": str(fx_trading_account_id(tenant.id, "EUR"))}
    if code == "period_closed":
        close_period(tenant, timezone.now() - datetime.timedelta(days=10))
        return {"effective_at": (timezone.now() - datetime.timedelta(days=11)).isoformat()}
    return {
        "invalid_rate": {"rate": 0.92},
        "invalid_rate_as_of": {"rate_as_of": "2999-01-01T00:00:00Z"},
        "invalid_amount": {"from_amount": "0.0001", "rate": "0.0001"},
        "insufficient_funds": {"from_amount": "5000.00"},
        "not_found": {"to_account_id": str(uuid.uuid4())},
    }[code]


@pytest.mark.django_db
@pytest.mark.parametrize("code", [
    "same_currency", "invalid_rate", "invalid_rate_as_of", "invalid_account",
    "invalid_amount", "insufficient_funds", "period_closed", "not_found",
])
def test_error_codes(api_client, auth, tenant, cash, code):
    usd, eur = cash
    resp = post_conversion(api_client, auth, body_for(usd, eur, **overrides_for(code, tenant)))
    assert resp.status_code == (404 if code == "not_found" else 422)
    assert resp.data["error"]["code"] == code


@pytest.mark.django_db
def test_idempotency_key_reuse(api_client, auth, cash):
    assert post_conversion(api_client, auth, body_for(*cash)).status_code == 201
    resp = post_conversion(api_client, auth, body_for(*cash, from_amount="50.00"))
    assert resp.status_code == 422
    assert resp.data["error"]["code"] == "idempotency_key_reuse"


@pytest.mark.django_db
def test_docs_still_render(api_client):
    assert api_client.get('/api/docs/').status_code == 200
    assert api_client.get('/api/schema/').status_code == 200
