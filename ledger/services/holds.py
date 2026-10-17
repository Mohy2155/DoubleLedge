import uuid
from decimal import Decimal
from django.db import transaction, connection
from django.db.models import Sum, F

from ledger.models import Account, Hold, OutboxEvent, IdempotencyKey
from ledger.services.idempotency import get_or_insert_idempotency_key
from ledger.services.posting import _direction_to_decrease, post_entry, retry_on_deadlock

class HoldError(Exception):
    pass

def compute_available_balance(account: Account, pending_holds_total: Decimal) -> Decimal:
    """
    Computes available balance for an account.
    pending_holds_total should be the sum of (amount - captured_amount) for PENDING holds.
    Since posted_balance is already signed correctly for the account's normal side,
    we subtract pending holds to get the available balance.
    """
    return account.balance - pending_holds_total

@retry_on_deadlock(max_retries=3, base_delay=0.05)
@transaction.atomic
def authorize_hold(tenant, payload: dict, idempotency_key: str, request_hash: bytes):
    key_row, created = get_or_insert_idempotency_key(
        tenant_id=tenant.id,
        key=idempotency_key,
        request_hash=request_hash
    )
    if not created:
        if bytes(key_row.request_hash) != request_hash:
            raise HoldError("idempotency_key_reuse")
        return key_row.response_code, key_row.response_body, True

    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.id)])
        cursor.execute("SET LOCAL lock_timeout = '2s'")

    try:
        account = Account.objects.select_for_update().get(
            tenant_id=tenant.id, id=payload['account_id']
        )
    except Account.DoesNotExist:
        raise HoldError("account_not_found")

    if account.currency != payload['currency']:
        raise HoldError("currency_mismatch")

    from django.utils.dateparse import parse_datetime
    from django.utils import timezone
    
    expires_at_raw = payload['expires_at']
    if isinstance(expires_at_raw, str):
        expires_at = parse_datetime(expires_at_raw)
    else:
        expires_at = expires_at_raw

    if not expires_at or expires_at <= timezone.now():
        raise HoldError("invalid_expiry")

    hold_amount = Decimal(str(payload['amount']))
    if hold_amount <= 0:
        raise HoldError("invalid_amount")

    from ledger.services.sharding import get_aggregate_balance
    aggs = get_aggregate_balance(tenant.id, account.id)
    available = aggs['available_balance']

    if available - hold_amount < 0 and (not account.allow_negative or account.shard_count > 0):
        raise HoldError("insufficient_funds")

    hold = Hold.objects.create(
        tenant=tenant,
        reference_id=payload['reference_id'],
        account=account,
        currency=account.currency,
        amount=hold_amount,
        description=payload.get('description', ''),
        expires_at=expires_at,
        metadata=payload.get('metadata', {}),
    )

    outbox_payload = {
        "hold_id": str(hold.id),
        "reference_id": hold.reference_id,
        "account_id": str(hold.account_id),
        "amount": str(hold.amount),
        "currency": hold.currency,
        "status": hold.status,
    }
    OutboxEvent.objects.create(tenant=tenant, type="hold.authorized", payload=outbox_payload)

    response_body = outbox_payload
    response_code = 201

    key_row.response_code = response_code
    key_row.response_body = response_body
    key_row.save(update_fields=['response_code', 'response_body'])

    return response_code, response_body, False

@retry_on_deadlock(max_retries=3, base_delay=0.05)
@transaction.atomic
def capture_hold(tenant, hold_id: str, payload: dict, idempotency_key: str, request_hash: bytes):
    key_row, created = get_or_insert_idempotency_key(
        tenant_id=tenant.id,
        key=idempotency_key,
        request_hash=request_hash
    )
    if not created:
        if bytes(key_row.request_hash) != request_hash:
            raise HoldError("idempotency_key_reuse")
        return key_row.response_code, key_row.response_body, True

    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.id)])
        cursor.execute("SET LOCAL lock_timeout = '2s'")

    try:
        hold = Hold.objects.select_for_update().get(tenant_id=tenant.id, id=hold_id)
    except Hold.DoesNotExist:
        raise HoldError("hold_not_found")

    if hold.status != 'PENDING':
        raise HoldError("hold_not_pending")

    remaining = hold.amount - hold.captured_amount
    capture_amount_str = payload.get('amount', str(remaining))
    try:
        capture_amount = Decimal(str(capture_amount_str))
    except (ValueError, TypeError):
        raise HoldError("invalid_amount_format")

    if capture_amount > remaining:
        raise HoldError("capture_exceeds_hold")
    if capture_amount <= 0:
        raise HoldError("invalid_amount")

    # hold.account is lazy loaded, let's select it for update inside post_entry. We just need its type.
    # actually, we can just read the type without locking it here, post_entry locks it.
    hold_account_dir = _direction_to_decrease(hold.account)
    dest_account_dir = 'DEBIT' if hold_account_dir == 'CREDIT' else 'CREDIT'

    posting_payload = {
        'reference_id': payload['reference_id'],
        'description': payload.get('description', f'Capture of hold {hold.reference_id}'),
        'lines': [
            {
                'account_id': str(hold.account_id),
                'amount': str(capture_amount),
                'direction': hold_account_dir,
            },
            {
                'account_id': str(payload['destination_account_id']),
                'amount': str(capture_amount),
                'direction': dest_account_dir,
            }
        ]
    }

    posting_idem_key = f"hold_capture:{hold.id}:{idempotency_key}" # The spec says: derived key is f"hold_capture:{hold.id}" to avoid collision
    posting_idem_key = f"hold_capture:{hold.id}" 
    import json, hashlib
    posting_hash = hashlib.sha256(json.dumps(posting_payload, sort_keys=True).encode()).digest()
    
    # We need to catch PostingError and wrap or propagate
    from ledger.services.posting import PostingError
    try:
        code, body, _ = post_entry(tenant, posting_payload, posting_idem_key, posting_hash)
    except PostingError as e:
        raise HoldError(str(e))

    hold.status = 'CAPTURED'
    hold.captured_amount = capture_amount
    hold.journal_entry_id = uuid.UUID(body['id'])
    hold.save(update_fields=['status', 'captured_amount', 'journal_entry_id', 'updated_at'])

    outbox_payload = {
        "hold_id": str(hold.id),
        "reference_id": hold.reference_id,
        "account_id": str(hold.account_id),
        "captured_amount": str(capture_amount),
        "released_amount": str(remaining - capture_amount),
        "journal_entry_id": str(hold.journal_entry_id)
    }
    OutboxEvent.objects.create(tenant=tenant, type="hold.captured", payload=outbox_payload)

    response_body = outbox_payload
    response_code = 201

    key_row.response_code = response_code
    key_row.response_body = response_body
    key_row.save(update_fields=['response_code', 'response_body'])

    return response_code, response_body, False

@retry_on_deadlock(max_retries=3, base_delay=0.05)
@transaction.atomic
def void_hold(tenant, hold_id: str, idempotency_key: str, request_hash: bytes):
    key_row, created = get_or_insert_idempotency_key(
        tenant_id=tenant.id,
        key=idempotency_key,
        request_hash=request_hash
    )
    if not created:
        if bytes(key_row.request_hash) != request_hash:
            raise HoldError("idempotency_key_reuse")
        return key_row.response_code, key_row.response_body, True

    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.id)])
        cursor.execute("SET LOCAL lock_timeout = '2s'")

    try:
        hold = Hold.objects.select_for_update().get(tenant_id=tenant.id, id=hold_id)
    except Hold.DoesNotExist:
        raise HoldError("hold_not_found")

    if hold.status != 'PENDING':
        raise HoldError("hold_not_pending")

    hold.status = 'VOIDED'
    hold.save(update_fields=['status', 'updated_at'])

    outbox_payload = {
        "hold_id": str(hold.id),
        "reference_id": hold.reference_id,
        "account_id": str(hold.account_id),
        "amount": str(hold.amount),
        "released_amount": str(hold.amount - hold.captured_amount),
    }
    OutboxEvent.objects.create(tenant=tenant, type="hold.voided", payload=outbox_payload)

    response_body = outbox_payload
    response_code = 200

    key_row.response_code = response_code
    key_row.response_body = response_body
    key_row.save(update_fields=['response_code', 'response_body'])

    return response_code, response_body, False
