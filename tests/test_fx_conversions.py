import datetime
import json
import threading
from decimal import Decimal

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.db import IntegrityError, connection
from django.utils import timezone
from rest_framework.test import APIClient

from api.utils import generate_idempotency_hash
from ledger.models import Account, FxConversion, IdempotencyKey, JournalEntry, OutboxEvent, Tenant, TransactionLine
from ledger.services.fx import (
    FxError,
    compute_to_amount,
    convert,
    ensure_trading_accounts,
    fx_trading_account_id,
)
from ledger.services.periods import close_period
from ledger.services.posting import PostingError
from ledger.services.sharding import create_sharded_account

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


def get_hash(payload):
    return generate_idempotency_hash('POST', '/api/v1/fx-conversions/', json.dumps(payload, default=str).encode())


def run_convert(tenant, from_account, to_account, key="fx-1", **overrides):
    payload = {
        'reference_id': f"ref-{key}",
        'from_account_id': str(from_account.id),
        'to_account_id': str(to_account.id),
        'from_amount': '100.0000',
        'rate': '0.9234',
        **overrides,
    }
    return convert(tenant, payload, key, get_hash(payload))


def trading_ids(tenant):
    return [fx_trading_account_id(tenant.id, ccy) for ccy in ("USD", "EUR")]


def assert_nothing_written(tenant):
    assert not JournalEntry.objects.filter(tenant=tenant).exists()
    assert not FxConversion.objects.filter(tenant=tenant).exists()
    assert not Account.objects.filter(id__in=trading_ids(tenant)).exists()
    assert not IdempotencyKey.objects.filter(tenant=tenant).exists()


def balances(*accounts):
    return [Account.objects.get(id=a.id if isinstance(a, Account) else a).balance for a in accounts]


def line_tuples(body):
    return [(line['account_id'], line['direction'], line['amount'], line['currency']) for line in body['lines']]


@pytest.fixture
def tenant():
    return Tenant.objects.create(name="FX Tenant")


@pytest.fixture
def cash(tenant):
    usd = Account.objects.create(tenant=tenant, name="Cash", type=Account.Type.ASSET, currency="USD", balance=Decimal("1000"))
    eur = Account.objects.create(tenant=tenant, name="Cash EUR", type=Account.Type.ASSET, currency="EUR")
    return usd, eur


# --- trading accounts and validation -------------------------------------------------

@pytest.mark.django_db
def test_trading_accounts_are_deterministic_and_created_once(tenant):
    first = ensure_trading_accounts(tenant.id, ["USD", "EUR"])
    second = ensure_trading_accounts(tenant.id, ["EUR", "USD"])
    assert first == second == sorted(trading_ids(tenant))
    assert fx_trading_account_id(tenant.id, "USD") == fx_trading_account_id(tenant.id, "USD")

    accounts = Account.objects.filter(tenant=tenant).order_by('name')
    assert [(a.name, a.type, a.currency, a.allow_negative) for a in accounts] == [
        ("FX Trading EUR", "EQUITY", "EUR", True),
        ("FX Trading USD", "EQUITY", "USD", True),
    ]


@pytest.mark.django_db
def test_trading_account_ids_differ_per_tenant(tenant):
    other = Tenant.objects.create(name="Other FX Tenant")
    assert fx_trading_account_id(tenant.id, "USD") != fx_trading_account_id(other.id, "USD")


def test_to_amount_rounds_half_up():
    assert compute_to_amount(Decimal("1.0000"), Decimal("0.12345")) == Decimal("0.1235")
    assert compute_to_amount(Decimal("1.0000"), Decimal("0.12344")) == Decimal("0.1234")


@pytest.mark.django_db
@pytest.mark.parametrize("from_amount, rate", [
    ("0.0001", "0.0001"),                # rounds to zero
    ("999999999999999.9999", "2"),       # beyond numeric(19,4)
])
def test_out_of_range_to_amount_is_invalid_amount(tenant, cash, from_amount, rate):
    with pytest.raises(FxError, match="^invalid_amount$"):
        run_convert(tenant, *cash, from_amount=from_amount, rate=rate)
    assert_nothing_written(tenant)


@pytest.mark.django_db
@pytest.mark.parametrize("rate", ["0", "-0.92", 0.92, 1, "0.1234567890123", "abc", "NaN", "1e16"])
def test_invalid_rate(tenant, cash, rate):
    with pytest.raises(FxError, match="^invalid_rate$"):
        run_convert(tenant, *cash, rate=rate)
    assert_nothing_written(tenant)


@pytest.mark.django_db
def test_future_rate_as_of(tenant, cash):
    with pytest.raises(FxError, match="^invalid_rate_as_of$"):
        run_convert(tenant, *cash, rate_as_of=timezone.now() + datetime.timedelta(minutes=5))
    assert_nothing_written(tenant)


@pytest.mark.django_db
def test_same_currency(tenant, cash):
    usd, _ = cash
    other_usd = Account.objects.create(tenant=tenant, name="Bank", type=Account.Type.ASSET, currency="USD")
    with pytest.raises(FxError, match="^same_currency$"):
        run_convert(tenant, usd, other_usd)
    assert_nothing_written(tenant)


@pytest.mark.django_db
@pytest.mark.parametrize("side", ["from", "to"])
def test_trading_account_cannot_be_source_or_destination(tenant, cash, side):
    usd, eur = cash
    ensure_trading_accounts(tenant.id, ["USD", "EUR"])
    if side == "from":
        source, destination = Account.objects.get(id=fx_trading_account_id(tenant.id, "USD")), eur
    else:
        source, destination = usd, Account.objects.get(id=fx_trading_account_id(tenant.id, "EUR"))
    with pytest.raises(FxError, match="^invalid_account$"):
        run_convert(tenant, source, destination)


@pytest.mark.django_db
def test_other_tenants_account_is_not_found(tenant, cash):
    usd, _ = cash
    other = Tenant.objects.create(name="Other FX Tenant")
    other_eur = Account.objects.create(tenant=other, name="Cash EUR", type=Account.Type.ASSET, currency="EUR")
    with pytest.raises(FxError, match="^account_not_found$"):
        run_convert(tenant, usd, other_eur)
    assert_nothing_written(tenant)


# --- posting the conversion ----------------------------------------------------------

@pytest.mark.django_db
def test_asset_to_asset_posts_four_lines_matching_the_stamp(tenant, cash):
    usd, eur = cash
    code, body, replayed = run_convert(tenant, usd, eur)
    assert (code, replayed) == (201, False)

    fx_usd, fx_eur = (str(i) for i in trading_ids(tenant))
    assert line_tuples(body) == [
        (str(usd.id), "CREDIT", "100.0000", "USD"),
        (fx_usd, "DEBIT", "100.0000", "USD"),
        (fx_eur, "CREDIT", "92.3400", "EUR"),
        (str(eur.id), "DEBIT", "92.3400", "EUR"),
    ]
    assert balances(usd, eur, fx_usd, fx_eur) == [Decimal("900"), Decimal("92.34"), Decimal("-100"), Decimal("92.34")]

    stamp = FxConversion.objects.get(tenant=tenant)
    assert body["id"] == str(stamp.id)
    assert body["journal_entry_id"] == str(stamp.journal_entry_id)
    assert (stamp.from_account_id, stamp.to_account_id) == (usd.id, eur.id)
    assert (stamp.from_currency, stamp.to_currency) == ("USD", "EUR")
    assert (stamp.from_amount, stamp.to_amount, stamp.rate) == (Decimal("100"), Decimal("92.34"), Decimal("0.9234"))
    assert (body["from_amount"], body["to_amount"], body["rate"], body["rate_source"]) == ("100.0000", "92.3400", "0.9234", "")


@pytest.mark.django_db
def test_liability_to_liability_decreases_and_increases_on_the_normal_side(tenant):
    usd = Account.objects.create(tenant=tenant, name="Deposits", type=Account.Type.LIABILITY, currency="USD", balance=Decimal("500"))
    eur = Account.objects.create(tenant=tenant, name="Deposits EUR", type=Account.Type.LIABILITY, currency="EUR")
    _, body, _ = run_convert(tenant, usd, eur)

    fx_usd, fx_eur = (str(i) for i in trading_ids(tenant))
    assert line_tuples(body) == [
        (str(usd.id), "DEBIT", "100.0000", "USD"),
        (fx_usd, "CREDIT", "100.0000", "USD"),
        (fx_eur, "DEBIT", "92.3400", "EUR"),
        (str(eur.id), "CREDIT", "92.3400", "EUR"),
    ]
    assert balances(usd, eur, fx_usd, fx_eur) == [Decimal("400"), Decimal("92.34"), Decimal("100"), Decimal("-92.34")]


@pytest.mark.django_db
def test_stamp_keeps_rate_source_and_rate_as_of(tenant, cash):
    rate_as_of = datetime.datetime(2026, 10, 16, 16, 0, tzinfo=datetime.timezone.utc)
    _, body, _ = run_convert(tenant, *cash, rate_source="ECB", rate_as_of=rate_as_of)
    assert (body["rate"], body["rate_source"], body["rate_as_of"]) == ("0.9234", "ECB", "2026-10-16T16:00:00Z")


@pytest.mark.django_db
def test_rate_as_of_defaults_to_processing_time(tenant, cash):
    before = timezone.now()
    run_convert(tenant, *cash)
    assert before <= FxConversion.objects.get(tenant=tenant).rate_as_of <= timezone.now()


@pytest.mark.django_db(transaction=True)
def test_concurrent_first_conversions_share_trading_accounts():
    tenant = Tenant.objects.create(name="FX Race Tenant")
    usd = Account.objects.create(tenant=tenant, name="Cash", type=Account.Type.ASSET, currency="USD", balance=Decimal("1000"))
    eur = Account.objects.create(tenant=tenant, name="Cash EUR", type=Account.Type.ASSET, currency="EUR")
    barrier = threading.Barrier(2)
    results = []

    def worker(i):
        try:
            connection.close()
            barrier.wait()
            results.append(run_convert(tenant, usd, eur, key=f"race-{i}"))
        except Exception as e:
            results.append(e)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2 and all(isinstance(r, tuple) and r[0] == 201 for r in results), results
    assert Account.objects.filter(tenant=tenant, name__startswith="FX Trading").count() == 2
    assert balances(usd, eur) == [Decimal("800"), Decimal("184.68")]


@pytest.mark.django_db
def test_insufficient_funds_writes_nothing(tenant, cash):
    with pytest.raises(IntegrityError, match="accounts_check"):
        run_convert(tenant, *cash, from_amount="5000.00")
    assert_nothing_written(tenant)


@pytest.mark.django_db
def test_closed_period(tenant, cash):
    cutoff = timezone.now() - datetime.timedelta(days=10)
    close_period(tenant, cutoff)
    with pytest.raises(PostingError, match="^period_closed$"):
        run_convert(tenant, *cash, effective_at=cutoff - datetime.timedelta(days=1))
    assert not FxConversion.objects.filter(tenant=tenant).exists()


@pytest.mark.django_db
def test_replay_and_key_reuse(tenant, cash):
    _, first, _ = run_convert(tenant, *cash, key="fx-replay")
    code, again, replayed = run_convert(tenant, *cash, key="fx-replay")
    assert (code, replayed) == (201, True)
    assert again == first
    assert JournalEntry.objects.filter(tenant=tenant).count() == 1

    with pytest.raises(FxError, match="^idempotency_key_reuse$"):
        run_convert(tenant, *cash, key="fx-replay", from_amount="50.00")


@pytest.mark.django_db
def test_entry_posted_event_is_emitted(tenant, cash):
    _, body, _ = run_convert(tenant, *cash)
    event = OutboxEvent.objects.get(tenant=tenant, type="entry.posted")
    assert event.payload["id"] == body["journal_entry_id"]


@pytest.mark.django_db
def test_sharded_source_routes_to_a_shard_and_reports_the_master(tenant, cash):
    _, eur = cash
    master = create_sharded_account(tenant.id, "Sharded Cash", Account.Type.ASSET, "USD", allow_negative=True, shard_count=4)
    _, body, _ = run_convert(tenant, master, eur)

    assert body["from_account_id"] == str(master.id)
    assert body["lines"][0]["account_id"] == str(master.id)
    source_line = TransactionLine.objects.select_related('account').get(
        journal_entry_id=body["journal_entry_id"], account__currency="USD", direction="CREDIT"
    )
    assert source_line.account.parent_account_id == master.id


@pytest.mark.django_db
def test_reversal_restores_all_four_balances_and_keeps_the_stamp(settings, tenant, cash):
    settings.JWT_PUBLIC_KEY = public_pem
    usd, eur = cash
    _, body, _ = run_convert(tenant, usd, eur)
    stamp_before = FxConversion.objects.filter(id=body["id"]).values().get()

    token = jwt.encode(
        {"tenant_id": str(tenant.id), "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)},
        private_pem, algorithm="RS256",
    )
    resp = APIClient().post(
        f"/api/v1/journal-entries/{body['journal_entry_id']}/reverse/", data={}, format='json',
        HTTP_IDEMPOTENCY_KEY="fx-reverse", HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert resp.status_code == 201

    fx_usd, fx_eur = trading_ids(tenant)
    assert balances(usd, eur, fx_usd, fx_eur) == [Decimal("1000"), Decimal("0"), Decimal("0"), Decimal("0")]
    assert FxConversion.objects.filter(id=body["id"]).values().get() == stamp_before
