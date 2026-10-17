import hashlib
import json
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from django.db import connection, transaction
from django.db.models import Prefetch
from django.utils import timezone

from ledger.models import Account, FxConversion, IdempotencyKey, TransactionLine
from ledger.services.idempotency import get_or_insert_idempotency_key
from ledger.services.periods import iso_utc
from ledger.services.posting import _direction_to_decrease, _direction_to_increase, post_entry, retry_on_deadlock


class FxError(Exception):
    pass

FOUR_PLACES = Decimal('0.0001')
MAX_AMOUNT = Decimal('999999999999999.9999')  # numeric(19,4)
MAX_RATE = Decimal(10) ** 16                   # numeric(28,12): at most 16 integer digits

def _opposite(direction: str) -> str:
    return 'CREDIT' if direction == 'DEBIT' else 'DEBIT'

def fx_trading_account_id(tenant_id, currency: str) -> uuid.UUID:
    """Deterministic id of the tenant's FX trading account for a currency (same pattern as shard ids)."""
    return uuid.uuid5(uuid.UUID(str(tenant_id)), f"fx_trading:{currency}")

def ensure_trading_accounts(tenant_id, currencies) -> list:
    """
    Creates any missing FX trading accounts. One statement in ascending id order,
    so concurrent first conversions wait on the same rows instead of deadlocking.
    """
    rows = sorted((fx_trading_account_id(tenant_id, ccy), ccy) for ccy in set(currencies))
    params = []
    for account_id, ccy in rows:
        params += [str(account_id), str(tenant_id), f"FX Trading {ccy}", ccy]
    values = ", ".join(["(%s, %s, %s, 'EQUITY', %s, true)"] * len(rows))
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO accounts (id, tenant_id, name, type, currency, allow_negative) VALUES {values} "
            "ON CONFLICT (id) DO NOTHING",
            params,
        )
    return [account_id for account_id, _ in rows]

def parse_from_amount(raw) -> Decimal:
    """Posting's precision rules for the amount leaving the source account."""
    if not isinstance(raw, str):
        raise FxError("invalid_amount_format")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        raise FxError("invalid_amount_format")
    if not amount.is_finite():
        raise FxError("invalid_amount_format")
    if amount.as_tuple().exponent < -4:
        raise FxError("invalid_amount_precision")
    if amount <= 0 or amount > MAX_AMOUNT:
        raise FxError("invalid_amount")
    return amount.quantize(FOUR_PLACES)

def parse_rate(raw) -> Decimal:
    """A positive decimal string that fits numeric(28,12). Every other input is invalid_rate."""
    if not isinstance(raw, str):
        raise FxError("invalid_rate")
    try:
        rate = Decimal(raw)
    except InvalidOperation:
        raise FxError("invalid_rate")
    if not rate.is_finite() or rate <= 0 or rate >= MAX_RATE or rate.as_tuple().exponent < -12:
        raise FxError("invalid_rate")
    return rate

def validate_rate_as_of(value):
    if value is None:
        return timezone.now()
    if value > timezone.now():
        raise FxError("invalid_rate_as_of")
    return value

def compute_to_amount(from_amount: Decimal, rate: Decimal) -> Decimal:
    """Half-up to 4 places in a context wide enough that the product is exact; fx_conversions CHECKs the same rule."""
    with localcontext(prec=60):
        to_amount = (from_amount * rate).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
    if to_amount <= 0 or to_amount > MAX_AMOUNT:
        raise FxError("invalid_amount")
    return to_amount

def format_rate(rate: Decimal) -> str:
    # numeric(28,12) pads with zeros; drop them so the rate reads as supplied (0.923400000000 -> 0.9234).
    return format(rate.normalize(), 'f')

def conversions_with_lines(tenant_id):
    return FxConversion.objects.filter(tenant_id=tenant_id).select_related('journal_entry').prefetch_related(
        Prefetch('journal_entry__lines', queryset=TransactionLine.objects.select_related('account').order_by('id'))
    )

def conversion_body(conversion: FxConversion) -> dict:
    """API shape of a conversion; expects a row from conversions_with_lines."""
    return {
        "id": str(conversion.id),
        "journal_entry_id": str(conversion.journal_entry_id),
        "from_account_id": str(conversion.from_account_id),
        "to_account_id": str(conversion.to_account_id),
        "from_currency": conversion.from_currency,
        "to_currency": conversion.to_currency,
        "from_amount": str(conversion.from_amount),
        "to_amount": str(conversion.to_amount),
        "rate": format_rate(conversion.rate),
        "rate_source": conversion.rate_source,
        "rate_as_of": iso_utc(conversion.rate_as_of),
        "effective_at": iso_utc(conversion.journal_entry.effective_at),
        "created_at": iso_utc(conversion.created_at),
        "lines": [
            {
                "account_id": str(line.account.parent_account_id or line.account_id),
                "direction": line.direction,
                "amount": str(line.amount),
                "currency": line.account.currency,
            }
            for line in conversion.journal_entry.lines.all()
        ],
    }

@retry_on_deadlock(max_retries=3, base_delay=0.05)
@transaction.atomic
def convert(tenant, payload: dict, idempotency_key: str, request_hash: bytes):
    key_row, created = get_or_insert_idempotency_key(tenant.id, idempotency_key, request_hash)
    if not created:
        if bytes(key_row.request_hash) != request_hash:
            raise FxError("idempotency_key_reuse")
        return key_row.response_code, key_row.response_body, True

    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.id)])
        cursor.execute("SET LOCAL lock_timeout = '2s'")

    # Shards are internal; like the accounts API, only masters are addressable.
    requested = {str(payload['from_account_id']), str(payload['to_account_id'])}
    accounts = {
        str(acc.id): acc
        for acc in Account.objects.filter(tenant_id=tenant.id, id__in=requested, parent_account_id__isnull=True)
    }
    if len(accounts) != len(requested):
        raise FxError("account_not_found")
    from_account = accounts[str(payload['from_account_id'])]
    to_account = accounts[str(payload['to_account_id'])]

    rate = parse_rate(payload['rate'])
    if from_account.currency == to_account.currency:
        raise FxError("same_currency")
    for acc in (from_account, to_account):
        if acc.id == fx_trading_account_id(tenant.id, acc.currency):
            raise FxError("invalid_account")
    rate_as_of = validate_rate_as_of(payload.get('rate_as_of'))

    from_amount = parse_from_amount(payload['from_amount'])
    to_amount = compute_to_amount(from_amount, rate)

    ensure_trading_accounts(tenant.id, [from_account.currency, to_account.currency])

    from_dir = _direction_to_decrease(from_account)
    to_dir = _direction_to_increase(to_account)
    posting_payload = {
        'reference_id': payload['reference_id'],
        'description': payload.get('description', ''),
        'effective_at': payload.get('effective_at'),
        'lines': [
            {'account_id': str(from_account.id), 'amount': str(from_amount), 'direction': from_dir},
            {'account_id': str(fx_trading_account_id(tenant.id, from_account.currency)), 'amount': str(from_amount), 'direction': _opposite(from_dir)},
            {'account_id': str(fx_trading_account_id(tenant.id, to_account.currency)), 'amount': str(to_amount), 'direction': _opposite(to_dir)},
            {'account_id': str(to_account.id), 'amount': str(to_amount), 'direction': to_dir},
        ],
    }
    posting_hash = hashlib.sha256(json.dumps(posting_payload, sort_keys=True, default=str).encode()).digest()
    _, entry_body, _ = post_entry(tenant, posting_payload, f"fx_conversion:{idempotency_key}", posting_hash)

    conversion = FxConversion.objects.create(
        tenant=tenant,
        journal_entry_id=entry_body['id'],
        from_account=from_account,
        to_account=to_account,
        from_currency=from_account.currency,
        to_currency=to_account.currency,
        from_amount=from_amount,
        to_amount=to_amount,
        rate=rate,
        rate_source=payload.get('rate_source', ''),
        rate_as_of=rate_as_of,
    )

    # Re-read so the stored response is exactly what retrieve returns later.
    response_body = conversion_body(conversions_with_lines(tenant.id).get(id=conversion.id))
    IdempotencyKey.objects.filter(tenant_id=tenant.id, key=idempotency_key).update(
        response_code=201, response_body=response_body
    )
    return 201, response_body, False
