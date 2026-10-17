import datetime
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.db import OperationalError, connection
from rest_framework.test import APIClient

import api.views
import ledger.services.fx
from ledger.models import Account, IdempotencyKey, JournalEntry, Tenant

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


@pytest.fixture(autouse=True)
def override_jwt_settings(settings):
    settings.JWT_PUBLIC_KEY = public_pem


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant():
    return Tenant.objects.create(name=f"Transient API Tenant {uuid.uuid4().hex[:8]}")


@pytest.fixture
def auth(tenant):
    token = jwt.encode(
        {"tenant_id": str(tenant.id), "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)},
        private_pem,
        algorithm="RS256",
    )
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.fixture
def usd_accounts(tenant):
    cash = Account.objects.create(tenant=tenant, name="Cash", type=Account.Type.ASSET, currency="USD", allow_negative=True)
    revenue = Account.objects.create(tenant=tenant, name="Revenue", type=Account.Type.REVENUE, currency="USD")
    return cash, revenue


def raise_sqlstate(code):
    with connection.cursor() as cursor:
        cursor.execute(f"DO $$ BEGIN RAISE EXCEPTION 'transient probe' USING ERRCODE = '{code}'; END $$;")


def failing_after(real, code, failures, calls):
    """Wrap a service: run it, then raise `code` on the first `failures` calls (so the failed attempt has written)."""
    def wrapper(*args, **kwargs):
        calls.append(1)
        result = real(*args, **kwargs)
        if len(calls) <= failures:
            raise_sqlstate(code)
        return result
    return wrapper


def always_raising(code, calls):
    def wrapper(*args, **kwargs):
        calls.append(1)
        raise_sqlstate(code)
    return wrapper


def entry_payload(cash, revenue, reference_id):
    return {
        "reference_id": reference_id,
        "lines": [
            {"account_id": str(cash.id), "amount": "25.0000", "direction": "DEBIT"},
            {"account_id": str(revenue.id), "amount": "25.0000", "direction": "CREDIT"},
        ],
    }


@pytest.mark.django_db(transaction=True)
def test_entry_retried_after_deadlock_is_applied_once(api_client, auth, tenant, usd_accounts, monkeypatch):
    calls = []
    monkeypatch.setattr(api.views, "post_entry", failing_after(api.views.post_entry, "40P01", 1, calls))

    resp = api_client.post(
        "/api/v1/journal-entries/", data=entry_payload(*usd_accounts, "retry-once"), format="json",
        HTTP_IDEMPOTENCY_KEY="retry-once-key", **auth,
    )

    assert resp.status_code == 201, resp.content
    assert len(calls) == 2
    assert JournalEntry.objects.filter(tenant=tenant, reference_id="retry-once").count() == 1
    assert IdempotencyKey.objects.filter(tenant=tenant, key="retry-once-key").count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("code", ["40P01", "40001", "55P03"])
def test_transient_error_on_every_attempt_returns_503(api_client, auth, usd_accounts, monkeypatch, code):
    calls = []
    monkeypatch.setattr(api.views, "post_entry", always_raising(code, calls))

    resp = api_client.post(
        "/api/v1/journal-entries/", data=entry_payload(*usd_accounts, f"always-{code}"), format="json",
        HTTP_IDEMPOTENCY_KEY=f"always-{code}", **auth,
    )

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "lock_timeout"
    assert resp["Retry-After"] == "1"
    assert len(calls) == 4  # the first attempt plus 3 retries


@pytest.mark.django_db(transaction=True)
def test_non_transient_error_is_not_retried_or_mapped(api_client, auth, usd_accounts, monkeypatch):
    calls = []
    monkeypatch.setattr(api.views, "post_entry", always_raising("57014", calls))  # query_canceled

    with pytest.raises(OperationalError):
        api_client.post(
            "/api/v1/journal-entries/", data=entry_payload(*usd_accounts, "non-transient"), format="json",
            HTTP_IDEMPOTENCY_KEY="non-transient", **auth,
        )
    assert len(calls) == 1


@pytest.mark.django_db(transaction=True)
def test_conversion_retried_as_whole_request(api_client, auth, tenant, monkeypatch):
    cash = Account.objects.create(tenant=tenant, name="Cash", type=Account.Type.ASSET, currency="USD", allow_negative=True)
    cash_eur = Account.objects.create(tenant=tenant, name="Cash EUR", type=Account.Type.ASSET, currency="EUR")
    calls = []
    # The nested post_entry deadlocks once; neither it nor convert() may retry, only the request does.
    monkeypatch.setattr(ledger.services.fx, "post_entry", failing_after(ledger.services.fx.post_entry, "40P01", 1, calls))

    resp = api_client.post(
        "/api/v1/fx-conversions/",
        data={
            "reference_id": "fx-retry", "from_account_id": str(cash.id), "to_account_id": str(cash_eur.id),
            "from_amount": "100.00", "rate": "0.92",
        },
        format="json", HTTP_IDEMPOTENCY_KEY="fx-retry-key", **auth,
    )

    assert resp.status_code == 201, resp.content
    assert resp.json()["to_amount"] == "92.0000"
    assert len(calls) == 2
    assert JournalEntry.objects.filter(tenant=tenant, reference_id="fx-retry").count() == 1
