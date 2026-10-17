"""
A close and a backdated posting for the same tenant must serialize (design D4):
whichever takes the per-tenant advisory lock first wins, and the loser waits.
"""
import datetime
import threading
import time
import uuid

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from ledger.models import Account, JournalEntry, PeriodClose, Tenant


@pytest.fixture
def tenant():
    return Tenant.objects.create(name="Period Race Tenant")


@pytest.fixture
def accounts(tenant):
    bank = Account.objects.create(tenant=tenant, name="Bank", type=Account.Type.ASSET, currency="USD", allow_negative=True)
    sales = Account.objects.create(tenant=tenant, name="Sales", type=Account.Type.REVENUE, currency="USD")
    return bank, sales


def _wait_for_advisory_waiter(cursor, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # pg_stat_activity is cached per transaction; drop the cache so each poll is live.
        cursor.execute("SELECT pg_stat_clear_snapshot()")
        cursor.execute("""
            SELECT count(*) FROM pg_stat_activity
             WHERE datname = current_database() AND wait_event_type = 'Lock' AND wait_event = 'advisory'
        """)
        if cursor.fetchone()[0] > 0:
            return True
        time.sleep(0.02)
    return False


def _post_backdated(tenant, accounts, effective_at):
    bank, sales = accounts
    entry_id = str(uuid.uuid4())
    with connection.cursor() as c:
        c.execute(
            "INSERT INTO journal_entries (id, tenant_id, reference_id, description, effective_at) VALUES (%s, %s, %s, '', %s)",
            [entry_id, str(tenant.id), f"race-{entry_id}", effective_at],
        )
        for account, direction in ((bank, 'DEBIT'), (sales, 'CREDIT')):
            c.execute(
                "INSERT INTO transaction_lines (tenant_id, journal_entry_id, account_id, direction, amount) VALUES (%s, %s, %s, %s, 10)",
                [str(tenant.id), entry_id, str(account.id), direction],
            )
    return entry_id


def _close(tenant, cutoff):
    with connection.cursor() as c:
        c.execute("SET LOCAL lock_timeout = '5s'")
        c.execute("INSERT INTO period_closes (tenant_id, cutoff, source) VALUES (%s, %s, 'MANUAL')", [str(tenant.id), cutoff])


def _run(*targets):
    errors = []

    def wrap(fn):
        def inner():
            try:
                fn()
            except Exception as exc:  # surfaced to the main thread below
                errors.append(exc)
            finally:
                connection.close()
        return inner

    threads = [threading.Thread(target=wrap(t)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    return errors


@pytest.mark.django_db(transaction=True)
def test_close_waits_for_in_flight_backdated_posting(tenant, accounts):
    cutoff = timezone.now() - datetime.timedelta(days=1)
    entry_inserted = threading.Event()
    observed = {}

    def posting():
        with transaction.atomic():
            observed['entry_id'] = _post_backdated(tenant, accounts, cutoff - datetime.timedelta(hours=1))
            entry_inserted.set()
            with connection.cursor() as c:
                observed['close_blocked'] = _wait_for_advisory_waiter(c)
        observed['posting_committed_at'] = time.monotonic()

    def closing():
        assert entry_inserted.wait(5)
        with transaction.atomic():
            _close(tenant, cutoff)
        observed['close_committed_at'] = time.monotonic()

    assert _run(posting, closing) == []
    assert observed['close_blocked'], "close did not wait for the in-flight posting"
    assert observed['close_committed_at'] > observed['posting_committed_at']
    assert JournalEntry.objects.filter(id=observed['entry_id']).exists()
    assert PeriodClose.objects.filter(tenant=tenant, cutoff=cutoff).exists()


@pytest.mark.django_db(transaction=True)
def test_backdated_posting_waits_for_close_then_is_rejected(tenant, accounts):
    cutoff = timezone.now() - datetime.timedelta(days=1)
    close_inserted = threading.Event()
    observed = {}

    def closing():
        with transaction.atomic():
            _close(tenant, cutoff)
            close_inserted.set()
            with connection.cursor() as c:
                observed['posting_blocked'] = _wait_for_advisory_waiter(c)

    def posting():
        assert close_inserted.wait(5)
        try:
            with transaction.atomic():
                with connection.cursor() as c:
                    c.execute("SET LOCAL lock_timeout = '5s'")
                _post_backdated(tenant, accounts, cutoff - datetime.timedelta(hours=1))
        except IntegrityError as exc:
            observed['error'] = str(exc)

    assert _run(closing, posting) == []
    assert observed['posting_blocked'], "posting did not wait for the in-flight close"
    assert observed.get('error', '').startswith('period_closed')
    assert not JournalEntry.objects.filter(tenant=tenant).exists()
