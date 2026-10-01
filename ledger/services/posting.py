import random
import time
from decimal import Decimal

from django.db import OperationalError, connection, transaction

from ledger.models import Account, IdempotencyKey, JournalEntry, OutboxEvent, TransactionLine
from ledger.services.idempotency import get_or_insert_idempotency_key


class PostingError(Exception):
    pass

def retry_on_deadlock(max_retries=5, base_delay=0.1):
    def decorator(func):
        def wrapper(*args, **kwargs):
            retries = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except OperationalError as e:
                    # psycopg3 exposes pgcode via e.__cause__.pgcode
                    cause = getattr(e, '__cause__', None)
                    pgcode = getattr(cause, 'pgcode', None)
                    if pgcode in ('40P01', '40001') and retries < max_retries:
                        retries += 1
                        time.sleep(base_delay * (2 ** retries) + random.uniform(0, 0.1))
                    else:
                        raise
        return wrapper
    return decorator

@retry_on_deadlock(max_retries=3, base_delay=0.05)
@transaction.atomic
def post_entry(tenant, payload, idempotency_key: str, request_hash: bytes):
    """
    Core posting service that safely constructs a double-entry ledger write.
    """
    # 1. Idempotency Check
    key_row, created = get_or_insert_idempotency_key(tenant.id, idempotency_key, request_hash)
    if not created:
        if bytes(key_row.request_hash) != request_hash:
            raise PostingError("idempotency_key_reuse")
        # For replay, we just return the stored response
        return key_row.response_code, key_row.response_body
    
    # 2. Database Session Setup
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.id)])
        cursor.execute("SET LOCAL lock_timeout = '2s'")
    
    # 3. Extract payload data
    reference_id = payload['reference_id']
    description = payload.get('description', '')
    lines_data = payload['lines']
    
    if len(lines_data) < 2:
        raise PostingError("Journal entry must have at least 2 lines")
    
    # Strictly check for floats and validate balances
    net = Decimal('0.0000')
    for line in lines_data:
        if isinstance(line['amount'], float):
            raise PostingError("Floats are strictly prohibited for monetary amounts.")
        
        amount_dec = Decimal(str(line['amount'])).quantize(Decimal('0.0001'))
        if amount_dec <= 0:
            raise PostingError("Amount must be strictly positive")
            
        line['amount_dec'] = amount_dec
        if line['direction'] == 'DEBIT':
            net += amount_dec
        else:
            net -= amount_dec
            
    if net != 0:
        raise PostingError("Journal entry is unbalanced")
    
    # 4. Deterministic Row Locking
    account_ids = list({line['account_id'] for line in lines_data})
    account_ids.sort() # Sort in ascending order to prevent deadlocks
    
    # Lock accounts
    accounts = Account.objects.select_for_update().filter(
        tenant_id=tenant.id, 
        id__in=account_ids
    ).order_by("id")
    
    accounts_by_id = {str(acc.id): acc for acc in accounts}
    
    if len(accounts_by_id) != len(account_ids):
        raise PostingError("One or more accounts not found")
        
    # 5. Insert Journal Entry
    entry = JournalEntry.objects.create(
        tenant=tenant,
        reference_id=reference_id,
        description=description
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
    response_body = {
        "id": str(entry.id),
        "reference_id": entry.reference_id,
        "status": "posted"
    }
    OutboxEvent.objects.create(
        tenant=tenant,
        type="entry.posted",
        payload=response_body
    )
    
    # 8. Record Idempotency Success
    IdempotencyKey.objects.filter(
        tenant_id=tenant.id, 
        key=idempotency_key
    ).update(
        response_code=201, 
        response_body=response_body
    )
    
    return 201, response_body
