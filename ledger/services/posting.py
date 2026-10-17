import logging
import random
import time
from decimal import Decimal
from functools import wraps

from django.db import OperationalError, connection, transaction
from django.utils import timezone

from ledger.models import (
    Account,
    IdempotencyKey,
    JournalEntry,
    OutboxEvent,
    TransactionLine,
)
from ledger.services.idempotency import get_or_insert_idempotency_key
from ledger.services.periods import get_cutoff, iso_utc

logger = logging.getLogger(__name__)

class PostingError(Exception):
    pass

def _is_internal_entry(entry: JournalEntry) -> bool:
    return bool(entry.metadata and entry.metadata.get("system"))

def exclude_internal_lines(qs):
    return qs.exclude(journal_entry__metadata__system__isnull=False)

def _direction_to_decrease(account: Account) -> str:
    if account.type in (Account.Type.ASSET, Account.Type.EXPENSE):
        return TransactionLine.Direction.CREDIT
    return TransactionLine.Direction.DEBIT

def _direction_to_increase(account: Account) -> str:
    if account.type in (Account.Type.ASSET, Account.Type.EXPENSE):
        return TransactionLine.Direction.DEBIT
    return TransactionLine.Direction.CREDIT

# deadlock_detected, serialization_failure, lock_not_available
TRANSIENT_SQLSTATES = ('40P01', '40001', '55P03')

def transient_sqlstate(exc):
    """The SQLSTATE of a retryable database error, else None. psycopg 3 puts it on the chained cause as `sqlstate`."""
    sqlstate = getattr(exc.__cause__, 'sqlstate', None)
    return sqlstate if sqlstate in TRANSIENT_SQLSTATES else None

def retry_on_deadlock(max_retries=5, base_delay=0.1):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Only the transaction's owner retries. Inside a caller's transaction a retry would run in a
            # savepoint that still holds the caller's locks, and nested retry loops would multiply attempts.
            if connection.in_atomic_block:
                return func(*args, **kwargs)
            retries = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except OperationalError as e:
                    sqlstate = transient_sqlstate(e)
                    if sqlstate and retries < max_retries:
                        retries += 1
                        logger.warning(f"{func.__name__}: transient error {sqlstate}, retry {retries}/{max_retries}")
                        time.sleep(base_delay * (2 ** retries) + random.uniform(0, 0.1))
                    else:
                        raise
        return wrapper
    return decorator

@retry_on_deadlock(max_retries=5, base_delay=0.1)
@transaction.atomic
def post_entry(tenant, payload, idempotency_key: str, request_hash: bytes, bypass_sharding: bool = False):
    """
    Core posting service that safely constructs a double-entry ledger write.
    """
    # 1. Idempotency Check
    key_row, created = get_or_insert_idempotency_key(tenant.id, idempotency_key, request_hash)
    if not created:
        if bytes(key_row.request_hash) != request_hash:
            raise PostingError("idempotency_key_reuse")
        # For replay, we just return the stored response
        return key_row.response_code, key_row.response_body, True
    
    # 2. Database Session Setup
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.id)])
        cursor.execute("SET LOCAL lock_timeout = '2s'")
    
    # 3. Extract payload data
    reference_id = payload['reference_id']
    description = payload.get('description', '')
    reverses_id = payload.get('reverses_id')
    lines_data = payload['lines']
    
    if len(lines_data) < 2:
        raise PostingError("unbalanced_entry")
    
    # Strictly check for floats and validate balances
    net = Decimal('0.0000')
    for line in lines_data:
        if not isinstance(line['amount'], str):
            raise PostingError("invalid_amount_format")
        
        from decimal import InvalidOperation
        try:
            amount_dec = Decimal(line['amount'])
        except (ValueError, TypeError, InvalidOperation):
            raise PostingError("invalid_amount_format")
            
        if amount_dec.as_tuple().exponent < -4:
            raise PostingError("invalid_amount_precision")
            
        if amount_dec <= 0:
            raise PostingError("invalid_amount")
            
        line['amount_dec'] = amount_dec
        if line['direction'] == 'DEBIT':
            net += amount_dec
        else:
            net -= amount_dec
            
    if net != 0:
        raise PostingError("unbalanced_entry")

    # 3.1. Business time. Friendly pre-check; trg_journal_entries_period_open is the backstop.
    effective_at = payload.get('effective_at')
    if effective_at is None:
        effective_at = timezone.now()
    elif effective_at > timezone.now():
        raise PostingError("invalid_effective_at")
    else:
        cutoff = get_cutoff(tenant.id)
        if cutoff is not None and effective_at < cutoff:
            raise PostingError("period_closed")
    
    # 3.5. Shard Routing
    # Reversals are exempt from routing; they must use the exact account_id provided.
    if not reverses_id and not bypass_sharding:
        import uuid
        requested_ids = list({line['account_id'] for line in lines_data})
        # Dirty read to detect sharding
        account_metas = Account.objects.filter(
            tenant_id=tenant.id, id__in=requested_ids
        ).values('id', 'shard_count')
        
        shard_map = {}
        for meta in account_metas:
            if meta['shard_count'] > 0:
                master_id = meta['id']
                shard_index = random.randint(0, meta['shard_count'] - 1)
                shard_map[str(master_id)] = str(uuid.uuid5(master_id, f"shard_{shard_index}"))
                
        for line in lines_data:
            orig_id = str(line['account_id'])
            if orig_id in shard_map:
                line['account_id'] = shard_map[orig_id]
                # Keep track of original for logical mapping in outbox
                line['logical_account_id'] = orig_id

    # 4. Deterministic Row Locking
    account_ids = list({str(line['account_id']) for line in lines_data})
    account_ids.sort() # Sort in ascending order to prevent deadlocks
    
    # Lock accounts
    accounts = Account.objects.select_for_update().filter(
        tenant_id=tenant.id, 
        id__in=account_ids
    ).order_by("id")
    
    accounts_by_id = {str(acc.id): acc for acc in accounts}
    
    if len(accounts_by_id) != len(account_ids):
        raise PostingError("One or more accounts not found")

    # 4.1. Per-currency balance. Friendly pre-check; the deferred trg_entry_balanced_* triggers are the backstop.
    net_by_currency = {}
    for line in lines_data:
        currency = accounts_by_id[str(line['account_id'])].currency
        signed = line['amount_dec'] if line['direction'] == 'DEBIT' else -line['amount_dec']
        net_by_currency[currency] = net_by_currency.get(currency, Decimal('0.0000')) + signed
    if any(net_by_currency.values()):
        raise PostingError("unbalanced_entry")

    # 5. Insert Journal Entry
    metadata = payload.get('metadata', {})
    entry = JournalEntry.objects.create(
        tenant=tenant,
        reference_id=reference_id,
        description=description,
        reverses_id=reverses_id,
        effective_at=effective_at,
        metadata=metadata
    )
    
    # 6. Insert Lines & Update Balances
    lines_to_create = []
    for line_data in lines_data:
        acc = accounts_by_id[str(line_data['account_id'])]
        amount = line_data['amount_dec']
        direction = line_data['direction']
        
        # Balance calculation based on normal side
        if direction == 'DEBIT':
            if acc.type in (Account.Type.ASSET, Account.Type.EXPENSE):
                acc.balance += amount
            else:
                acc.balance -= amount
        elif direction == 'CREDIT':
            if acc.type in (Account.Type.LIABILITY, Account.Type.EQUITY, Account.Type.REVENUE):
                acc.balance += amount
            else:
                acc.balance -= amount
        
        # Increment version for OCC
        acc.version += 1
        
        lines_to_create.append(TransactionLine(
            tenant=tenant,
            journal_entry=entry,
            account=acc,
            direction=direction,
            amount=amount
        ))
    
    TransactionLine.objects.bulk_create(lines_to_create)
    Account.objects.bulk_update(accounts, ['balance', 'version'])
    
    # 7. Insert Outbox Event
    if not _is_internal_entry(entry):
        event_type = "entry.reversed" if reverses_id else "entry.posted"
        response_body = {
            "id": str(entry.id),
            "reference_id": entry.reference_id,
            "description": entry.description,
            "reverses_id": str(entry.reverses_id) if entry.reverses_id else None,
            "effective_at": iso_utc(entry.effective_at),
            "lines": [
                {
                    "account_id": str(line.account.parent_account_id) if line.account.parent_account_id else str(line.account.id),
                    "direction": line.direction,
                    "amount": str(line.amount)
                }
                for line in lines_to_create
            ]
        }
        OutboxEvent.objects.create(
            tenant=tenant,
            type=event_type,
            payload=response_body
        )
    else:
        # Build response_body anyway to return it, even though we don't emit an event
        response_body = {
            "id": str(entry.id),
            "reference_id": entry.reference_id,
            "description": entry.description,
            "reverses_id": str(entry.reverses_id) if entry.reverses_id else None,
            "effective_at": iso_utc(entry.effective_at),
            "lines": [
                {
                    "account_id": str(line.account.parent_account_id) if line.account.parent_account_id else str(line.account.id),
                    "direction": line.direction,
                    "amount": str(line.amount)
                }
                for line in lines_to_create
            ]
        }
    
    # 8. Record Idempotency Success
    IdempotencyKey.objects.filter(
        tenant_id=tenant.id, 
        key=idempotency_key
    ).update(
        response_code=201, 
        response_body=response_body
    )
    
    return 201, response_body, False
