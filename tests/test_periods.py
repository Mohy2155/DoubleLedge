import datetime
from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

from ledger.models import Account, IdempotencyKey, JournalEntry, OutboxEvent, PeriodClose, Tenant
from ledger.services import periods
from ledger.services.periods import PeriodCloseError, close_period, get_cutoff, scheduled_cutoff
from ledger.services.posting import PostingError, post_entry

UTC = datetime.timezone.utc


def dt(*args):
    return datetime.datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize("now, grace, expected", [
    (dt(2026, 11, 6, 0, 30), 5, dt(2026, 11, 1)),     # grace elapsed
    (dt(2026, 11, 3, 0, 30), 5, dt(2026, 10, 1)),     # grace not elapsed: October stays open
    (dt(2026, 11, 1, 0, 0), 0, dt(2026, 11, 1)),      # grace 0 closes at the boundary
    (dt(2026, 3, 27, 23, 59), 27, dt(2026, 2, 1)),    # Feb close not yet due on day 27
    (dt(2026, 3, 28, 0, 0), 27, dt(2026, 3, 1)),      # ... due on day 28
    (dt(2027, 1, 2, 0, 0), 5, dt(2026, 12, 1)),       # December -> January rollover
    (datetime.datetime(2026, 11, 6, 2, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=5))), 5,
     dt(2026, 10, 1)),                                # Nov 6 at +05:00 is still Nov 5 in UTC
])
def test_scheduled_cutoff(now, grace, expected):
    assert scheduled_cutoff(now, grace) == expected


@pytest.fixture
def tenant():
    return Tenant.objects.create(name="Periods Tenant")


@pytest.fixture
def accounts(tenant):
    bank = Account.objects.create(tenant=tenant, name="Bank", type=Account.Type.ASSET, currency="USD", allow_negative=True)
    sales = Account.objects.create(tenant=tenant, name="Sales", type=Account.Type.REVENUE, currency="USD")
    return bank, sales


def payload(accounts, ref, effective_at=None):
    bank, sales = accounts
    body = {
        "reference_id": ref,
        "description": "Sale",
        "lines": [
            {"account_id": str(bank.id), "amount": "10.00", "direction": "DEBIT"},
            {"account_id": str(sales.id), "amount": "10.00", "direction": "CREDIT"},
        ],
    }
    if effective_at is not None:
        body["effective_at"] = effective_at
    return body


CUTOFF = timezone.now().replace(microsecond=0) - datetime.timedelta(days=10)


@pytest.mark.django_db
def test_close_period_records_and_advances(tenant):
    assert get_cutoff(tenant.id) is None
    close = close_period(tenant, CUTOFF)
    assert close.source == PeriodClose.Source.MANUAL
    assert get_cutoff(tenant.id) == CUTOFF


@pytest.mark.django_db
@pytest.mark.parametrize("cutoff", [CUTOFF, CUTOFF - datetime.timedelta(days=1), timezone.now() + datetime.timedelta(hours=1)])
def test_close_period_rejects_invalid_cutoff(tenant, cutoff):
    if cutoff <= CUTOFF:
        close_period(tenant, CUTOFF)
    with pytest.raises(PeriodCloseError, match="invalid_period_cutoff"):
        close_period(tenant, cutoff)


@pytest.mark.django_db
def test_close_period_maps_trigger_rejection(tenant, monkeypatch):
    close_period(tenant, CUTOFF)
    # Simulate losing a race: the pre-check saw no cutoff, the trigger sees the committed one.
    monkeypatch.setattr(periods, "get_cutoff", lambda tenant_id: None)
    with pytest.raises(PeriodCloseError, match="invalid_period_cutoff"):
        with transaction.atomic():
            close_period(tenant, CUTOFF)


@pytest.mark.django_db
def test_backdated_posting_into_open_period(tenant, accounts):
    effective_at = timezone.now() - datetime.timedelta(days=3)
    code, body, replayed = post_entry(tenant, payload(accounts, "BACKDATED", effective_at), "idem-bd", b"h-bd")
    assert (code, replayed) == (201, False)
    entry = JournalEntry.objects.get(id=body["id"])
    assert entry.effective_at == effective_at
    assert entry.posted_at > effective_at
    assert body["effective_at"] == effective_at.isoformat().replace("+00:00", "Z")
    assert OutboxEvent.objects.get(tenant=tenant, type="entry.posted").payload["effective_at"] == body["effective_at"]


@pytest.mark.django_db
def test_default_effective_at_is_now(tenant, accounts):
    before = timezone.now()
    _, body, _ = post_entry(tenant, payload(accounts, "NOW"), "idem-now", b"h-now")
    assert JournalEntry.objects.get(id=body["id"]).effective_at >= before


@pytest.mark.django_db
def test_future_effective_at_is_rejected(tenant, accounts):
    with pytest.raises(PostingError, match="invalid_effective_at"):
        post_entry(tenant, payload(accounts, "FUTURE", timezone.now() + datetime.timedelta(minutes=5)), "idem-f", b"h-f")


@pytest.mark.django_db
def test_posting_into_closed_period_writes_nothing(tenant, accounts):
    bank, sales = accounts
    close_period(tenant, CUTOFF)
    with pytest.raises(PostingError, match="period_closed"):
        post_entry(tenant, payload(accounts, "CLOSED", CUTOFF - datetime.timedelta(seconds=1)), "idem-c", b"h-c")
    assert not JournalEntry.objects.filter(tenant=tenant).exists()
    assert not OutboxEvent.objects.filter(tenant=tenant).exists()
    assert not IdempotencyKey.objects.filter(tenant=tenant, key="idem-c").exists()
    bank.refresh_from_db()
    sales.refresh_from_db()
    assert bank.balance == sales.balance == Decimal("0")


@pytest.mark.django_db
def test_replay_of_pre_close_success_still_returns_201(tenant, accounts):
    body_in = payload(accounts, "PRE-CLOSE", CUTOFF - datetime.timedelta(days=1))
    _, first, _ = post_entry(tenant, body_in, "idem-pre", b"h-pre")
    close_period(tenant, CUTOFF)
    code, replay, replayed = post_entry(tenant, body_in, "idem-pre", b"h-pre")
    assert (code, replayed, replay) == (201, True, first)


@pytest.mark.django_db
def test_reversal_of_closed_period_entry_is_dated_now(tenant, accounts):
    _, original, _ = post_entry(tenant, payload(accounts, "OLD", CUTOFF - datetime.timedelta(days=1)), "idem-old", b"h-old")
    close_period(tenant, CUTOFF)
    reversal = {
        "reference_id": "reversal:OLD",
        "description": "Reversal of OLD",
        "reverses_id": original["id"],
        "lines": [
            {"account_id": l["account_id"], "amount": l["amount"], "direction": "CREDIT" if l["direction"] == "DEBIT" else "DEBIT"}
            for l in original["lines"]
        ],
    }
    before = timezone.now()
    code, body, _ = post_entry(tenant, reversal, "idem-rev", b"h-rev")
    assert code == 201
    assert JournalEntry.objects.get(id=body["id"]).effective_at >= before


# --- scheduled close task -------------------------------------------------------------

@pytest.fixture
def frozen_now(monkeypatch):
    """Freezes 'now' for the periods service only; must stay in the past for the DB's now() checks."""
    from types import SimpleNamespace

    def freeze(value):
        monkeypatch.setattr(periods, "timezone", SimpleNamespace(now=lambda: value))
    return freeze


def with_policy(name, grace_days):
    tenant = Tenant.objects.create(name=name)
    Tenant.objects.filter(id=tenant.id).update(period_close_grace_days=grace_days)
    return tenant


@pytest.mark.django_db
def test_scheduled_close_closes_policy_tenants_only(frozen_now):
    from workers.tasks import close_due_periods
    frozen_now(dt(2025, 3, 6, 0, 15))
    with_grace = with_policy("Grace 5", 5)
    not_due = with_policy("Grace 10", 10)
    no_policy = Tenant.objects.create(name="No policy")

    close_due_periods()

    close = PeriodClose.objects.get(tenant=with_grace)
    assert (close.cutoff, close.source) == (dt(2025, 3, 1), PeriodClose.Source.SCHEDULED)
    assert get_cutoff(not_due.id) == dt(2025, 2, 1)
    assert not PeriodClose.objects.filter(tenant=no_policy).exists()

    close_due_periods()  # second run is a no-op
    assert PeriodClose.objects.filter(tenant=with_grace).count() == 1
    assert PeriodClose.objects.filter(tenant=not_due).count() == 1


@pytest.mark.django_db
def test_scheduled_close_catches_up_in_one_close(frozen_now):
    from workers.tasks import close_due_periods
    tenant = with_policy("Catch-up", 5)
    close_period(tenant, dt(2024, 11, 1))
    frozen_now(dt(2025, 3, 6, 0, 15))

    close_due_periods()

    assert list(PeriodClose.objects.filter(tenant=tenant).order_by('cutoff').values_list('cutoff', 'source')) == [
        (dt(2024, 11, 1), PeriodClose.Source.MANUAL),
        (dt(2025, 3, 1), PeriodClose.Source.SCHEDULED),
    ]


# --- set_period_close_policy command --------------------------------------------------

@pytest.mark.django_db
def test_set_period_close_policy_command(tenant):
    from django.core.management import call_command

    call_command('set_period_close_policy', '--tenant_id', str(tenant.id), '--grace_days', '5')
    tenant.refresh_from_db()
    assert tenant.period_close_grace_days == 5

    call_command('set_period_close_policy', '--tenant_id', str(tenant.id), '--disable')
    tenant.refresh_from_db()
    assert tenant.period_close_grace_days is None


@pytest.mark.django_db
@pytest.mark.parametrize("args, message", [
    (['--grace_days', '28'], "between 0 and 27"),
    (['--grace_days', '-1'], "between 0 and 27"),
])
def test_set_period_close_policy_rejects_out_of_range(tenant, args, message):
    from django.core.management import CommandError, call_command

    with pytest.raises(CommandError, match=message):
        call_command('set_period_close_policy', '--tenant_id', str(tenant.id), *args)


@pytest.mark.django_db
def test_set_period_close_policy_unknown_tenant():
    import uuid
    from django.core.management import CommandError, call_command

    with pytest.raises(CommandError, match="does not exist"):
        call_command('set_period_close_policy', '--tenant_id', str(uuid.uuid4()), '--grace_days', '5')
