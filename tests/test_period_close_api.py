import datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.utils import timezone
from rest_framework.test import APIClient

from ledger.models import Account, PeriodClose, Tenant
from ledger.services import posting

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

CUTOFF = timezone.now().replace(microsecond=0) - datetime.timedelta(days=10)


def iso(value):
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


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
    return Tenant.objects.create(name="Period API Tenant")


@pytest.fixture
def auth(tenant):
    return headers_for(tenant)


@pytest.fixture
def accounts(tenant):
    bank = Account.objects.create(tenant=tenant, name="Bank", type=Account.Type.ASSET, currency="USD", allow_negative=True)
    sales = Account.objects.create(tenant=tenant, name="Sales", type=Account.Type.REVENUE, currency="USD")
    return bank, sales


def entry_payload(accounts, ref, effective_at=None):
    bank, sales = accounts
    body = {
        "reference_id": ref,
        "lines": [
            {"account_id": str(bank.id), "amount": "25.00", "direction": "DEBIT"},
            {"account_id": str(sales.id), "amount": "25.00", "direction": "CREDIT"},
        ],
    }
    if effective_at is not None:
        body["effective_at"] = effective_at
    return body


def post_entry(client, auth, body, key):
    return client.post('/api/v1/journal-entries/', data=body, format='json', HTTP_IDEMPOTENCY_KEY=key, **auth)


def post_close(client, auth, cutoff, key):
    return client.post('/api/v1/period-closes/', data={"cutoff": cutoff}, format='json', HTTP_IDEMPOTENCY_KEY=key, **auth)


# --- effective_at on journal entries -------------------------------------------------

@pytest.mark.django_db
def test_backdated_create_and_retrieve(api_client, auth, accounts):
    effective_at = iso(timezone.now().replace(microsecond=0) - datetime.timedelta(days=3))
    resp = post_entry(api_client, auth, entry_payload(accounts, "BD-1", effective_at), "k-bd-1")
    assert resp.status_code == 201
    assert resp.data["effective_at"] == effective_at

    resp = api_client.get(f"/api/v1/journal-entries/{resp.data['id']}/", **auth)
    assert resp.status_code == 200
    assert resp.data["effective_at"] == effective_at


@pytest.mark.django_db
def test_naive_effective_at_is_400(api_client, auth, accounts):
    resp = post_entry(api_client, auth, entry_payload(accounts, "NAIVE", "2026-09-30T23:00:00"), "k-naive")
    assert resp.status_code == 400
    assert resp.data["error"]["code"] == "invalid"


@pytest.mark.django_db
def test_future_effective_at_is_422(api_client, auth, accounts):
    future = iso(timezone.now() + datetime.timedelta(hours=1))
    resp = post_entry(api_client, auth, entry_payload(accounts, "FUT", future), "k-fut")
    assert resp.status_code == 422
    assert resp.data["error"]["code"] == "invalid_effective_at"


@pytest.mark.django_db
def test_closed_period_is_422(api_client, auth, accounts):
    assert post_close(api_client, auth, iso(CUTOFF), "k-close").status_code == 201
    resp = post_entry(api_client, auth, entry_payload(accounts, "LATE", iso(CUTOFF - datetime.timedelta(days=1))), "k-late")
    assert resp.status_code == 422
    assert resp.data["error"]["code"] == "period_closed"


@pytest.mark.django_db
def test_closed_period_trigger_backstop_maps_to_422(api_client, auth, accounts, monkeypatch):
    assert post_close(api_client, auth, iso(CUTOFF), "k-close").status_code == 201
    # Bypass the service pre-check so the DB trigger is what rejects the write.
    monkeypatch.setattr(posting, "get_cutoff", lambda tenant_id: None)
    resp = post_entry(api_client, auth, entry_payload(accounts, "LATE", iso(CUTOFF - datetime.timedelta(days=1))), "k-late")
    assert resp.status_code == 422
    assert resp.data["error"]["code"] == "period_closed"


# --- period-closes endpoints ---------------------------------------------------------

@pytest.mark.django_db
def test_close_and_idempotent_replay(api_client, auth, tenant):
    resp = post_close(api_client, auth, iso(CUTOFF), "k-c1")
    assert resp.status_code == 201
    assert resp.data["cutoff"] == iso(CUTOFF)
    assert resp.data["source"] == "MANUAL"
    assert {"id", "created_at"} <= resp.data.keys()

    replay = post_close(api_client, auth, iso(CUTOFF), "k-c1")
    assert replay.status_code == 201
    assert replay.headers.get("Idempotent-Replayed") == "true"
    assert replay.data == resp.data
    assert PeriodClose.objects.filter(tenant=tenant).count() == 1


@pytest.mark.django_db
def test_close_requires_idempotency_key(api_client, auth):
    resp = api_client.post('/api/v1/period-closes/', data={"cutoff": iso(CUTOFF)}, format='json', **auth)
    assert resp.status_code == 400
    assert resp.data["error"]["code"] == "missing_idempotency_key"


@pytest.mark.django_db
@pytest.mark.parametrize("cutoff", [CUTOFF, CUTOFF - datetime.timedelta(days=1), timezone.now() + datetime.timedelta(days=1)])
def test_invalid_cutoff_is_422(api_client, auth, cutoff):
    if cutoff <= CUTOFF:
        assert post_close(api_client, auth, iso(CUTOFF), "k-first").status_code == 201
    resp = post_close(api_client, auth, iso(cutoff), "k-second")
    assert resp.status_code == 422
    assert resp.data["error"]["code"] == "invalid_period_cutoff"


@pytest.mark.django_db
def test_list_is_newest_first_and_tenant_isolated(api_client, auth):
    empty = api_client.get('/api/v1/period-closes/', **auth)
    assert empty.status_code == 200
    assert empty.data == {"current_cutoff": None, "results": []}

    older, newer = CUTOFF - datetime.timedelta(days=30), CUTOFF
    post_close(api_client, auth, iso(older), "k-old")
    post_close(api_client, auth, iso(newer), "k-new")

    resp = api_client.get('/api/v1/period-closes/', **auth)
    assert resp.data["current_cutoff"] == iso(newer)
    assert [c["cutoff"] for c in resp.data["results"]] == [iso(newer), iso(older)]

    other = Tenant.objects.create(name="Other Period Tenant")
    resp = api_client.get('/api/v1/period-closes/', **headers_for(other))
    assert resp.data == {"current_cutoff": None, "results": []}
