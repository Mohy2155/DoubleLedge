import logging
import threading
import time
import uuid

import pytest
from django.db import OperationalError, connection, transaction

from ledger.models import Account, JournalEntry, Tenant
from ledger.services.posting import post_entry, retry_on_deadlock


def raise_sqlstate(code):
    # A genuine server error with the given SQLSTATE: same Django wrapping and psycopg class as a real one.
    with connection.cursor() as cursor:
        cursor.execute(f"DO $$ BEGIN RAISE EXCEPTION 'transient probe' USING ERRCODE = '{code}'; END $$;")


def make_flaky(code, failures):
    """Decorated function: each attempt writes a tenant row, and the first `failures` attempts then raise `code`.

    Rows carry a per-call prefix because the unmanaged tenants table survives transactional test flushes.
    """
    calls = []
    prefix = f"retry-probe-{uuid.uuid4().hex[:8]}-"

    @retry_on_deadlock(max_retries=2, base_delay=0.001)
    @transaction.atomic
    def flaky():
        calls.append(1)
        Tenant.objects.create(name=f"{prefix}{len(calls)}")
        if len(calls) <= failures:
            raise_sqlstate(code)
        return "ok"

    def attempts_persisted():
        return sorted(name.removeprefix(prefix) for name in Tenant.objects.filter(name__startswith=prefix).values_list("name", flat=True))

    flaky.attempts_persisted = attempts_persisted
    return flaky, calls


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("code", ["40P01", "40001", "55P03"])
def test_transient_error_is_retried_and_failed_attempt_rolled_back(code):
    flaky, calls = make_flaky(code, failures=1)

    assert flaky() == "ok"
    assert len(calls) == 2
    assert flaky.attempts_persisted() == ["2"]


@pytest.mark.django_db(transaction=True)
def test_non_transient_operational_error_is_not_retried():
    flaky, calls = make_flaky("57014", failures=1)  # query_canceled

    with pytest.raises(OperationalError):
        flaky()
    assert len(calls) == 1
    assert flaky.attempts_persisted() == []


@pytest.mark.django_db(transaction=True)
def test_retries_exhausted_reraises_and_persists_nothing():
    flaky, calls = make_flaky("40P01", failures=99)

    with pytest.raises(OperationalError) as exc_info:
        flaky()
    assert exc_info.value.__cause__.sqlstate == "40P01"
    assert len(calls) == 3  # max_retries=2, plus the first attempt
    assert flaky.attempts_persisted() == []


@pytest.mark.django_db(transaction=True)
def test_call_inside_callers_transaction_is_not_retried():
    flaky, calls = make_flaky("40P01", failures=1)

    with pytest.raises(OperationalError):
        with transaction.atomic():
            flaky()
    assert len(calls) == 1
    assert flaky.attempts_persisted() == []


@pytest.mark.django_db(transaction=True)
def test_posting_blocked_past_lock_timeout_succeeds_on_retry(caplog):
    tenant = Tenant.objects.create(name="Lock Wait Tenant")
    source = Account.objects.create(tenant=tenant, name="Source", type=Account.Type.ASSET, currency="USD", allow_negative=True)
    target = Account.objects.create(tenant=tenant, name="Target", type=Account.Type.REVENUE, currency="USD")
    locked = threading.Event()

    def hold_source_lock():
        try:
            with transaction.atomic():
                Account.objects.select_for_update().get(id=source.id)
                locked.set()
                time.sleep(2.5)  # longer than post_entry's 2s lock_timeout
        finally:
            connection.close()

    holder = threading.Thread(target=hold_source_lock)
    holder.start()
    assert locked.wait(10)

    payload = {
        "reference_id": "lock-wait",
        "lines": [
            {"account_id": str(source.id), "amount": "10.0000", "direction": "DEBIT"},
            {"account_id": str(target.id), "amount": "10.0000", "direction": "CREDIT"},
        ],
    }
    caplog.set_level(logging.WARNING, logger="ledger.services.posting")
    started = time.monotonic()
    code, _, _ = post_entry(tenant, payload, "lock-wait-key", b"lock-wait-hash")
    elapsed = time.monotonic() - started
    holder.join()

    assert code == 201
    assert JournalEntry.objects.filter(tenant=tenant, reference_id="lock-wait").count() == 1
    assert elapsed > 2.0
    retries = [r.getMessage() for r in caplog.records if r.name == "ledger.services.posting"]
    assert len(retries) == 1 and "55P03" in retries[0], retries
