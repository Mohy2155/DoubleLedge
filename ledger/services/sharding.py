import uuid
from typing import List, Optional
from decimal import Decimal
from django.db import transaction
from ledger.models import Account, Tenant

def create_sharded_account(
    tenant_id: uuid.UUID,
    name: str,
    account_type: str,
    currency: str,
    allow_negative: bool = False,
    shard_count: int = 0
) -> Account:
    """
    Creates a new account. If shard_count > 0, also atomically creates the specified
    number of shard accounts with deterministic uuid5 ids.
    """
    if shard_count < 0 or shard_count > 64:
        raise ValueError("shard_count must be between 0 and 64")

    if shard_count > 0 and not allow_negative:
        from rest_framework.exceptions import ValidationError
        raise ValidationError("Sharding is mutually exclusive with strict overdraft prevention (allow_negative=False).")

    master_id = uuid.uuid4()
    
    with transaction.atomic():
        master_account = Account.objects.create(
            id=master_id,
            tenant_id=tenant_id,
            name=name,
            type=account_type,
            currency=currency,
            allow_negative=allow_negative,
            shard_count=shard_count,
            parent_account_id=None
        )

        if shard_count > 0:
            shards = []
            for i in range(shard_count):
                shard_id = uuid.uuid5(master_id, f"shard_{i}")
                shards.append(
                    Account(
                        id=shard_id,
                        tenant_id=tenant_id,
                        name=f"{name} (Shard {i})",
                        type=account_type,
                        currency=currency,
                        allow_negative=True,  # Shards MUST be True to allow post-sweep reversals
                        shard_count=0,
                        parent_account_id=master_id
                    )
                )
            Account.objects.bulk_create(shards)

    return master_account

def resolve_member_ids(tenant_id: uuid.UUID, account_id: uuid.UUID) -> List[uuid.UUID]:
    """
    Returns the master account ID and all shard IDs if the account is sharded.
    Otherwise returns just the account ID.
    """
    account_meta = Account.objects.filter(tenant_id=tenant_id, id=account_id).values('shard_count', 'parent_account_id').first()
    if not account_meta:
        return [account_id]
        
    if account_meta['parent_account_id']:
        # It's a shard, but shards shouldn't be resolved as masters.
        return [account_id]

    shard_count = account_meta['shard_count']
    if shard_count == 0:
        return [account_id]

    return [account_id] + [uuid.uuid5(account_id, f"shard_{i}") for i in range(shard_count)]

def get_aggregate_balance(tenant_id: uuid.UUID, account_id: uuid.UUID) -> dict:
    """
    Returns the aggregated balance and pending holds across the master and all its shards.
    """
    member_ids = resolve_member_ids(tenant_id, account_id)
    
    from django.db.models import Sum, F
    
    aggs = Account.objects.filter(tenant_id=tenant_id, id__in=member_ids).aggregate(
        total_balance=Sum('balance')
    )
    
    total_balance = aggs['total_balance'] or Decimal('0.0000')
    
    from ledger.models import Hold
    pending_holds = Hold.objects.filter(
        tenant_id=tenant_id, account_id__in=member_ids, status='PENDING'
    ).aggregate(
        total=Sum(F('amount') - F('captured_amount'))
    )['total'] or Decimal('0.0000')
    
    return {
        "posted_balance": total_balance,
        "pending_holds": pending_holds,
        "available_balance": total_balance - pending_holds
    }

def to_logical_account_id(tenant_id: uuid.UUID, account_ids: List[uuid.UUID]) -> dict:
    """
    Given a list of physical account IDs (which may be shards or masters),
    returns a dictionary mapping physical_id -> logical_id (master_id).
    """
    if not account_ids:
        return {}
        
    metas = Account.objects.filter(tenant_id=tenant_id, id__in=account_ids).values('id', 'parent_account_id')
    mapping = {}
    for meta in metas:
        physical_id = str(meta['id'])
        if meta['parent_account_id']:
            mapping[physical_id] = str(meta['parent_account_id'])
        else:
            mapping[physical_id] = physical_id
            
    # For any requested ids that weren't in the DB, just map them to themselves
    for acc_id in account_ids:
        acc_str = str(acc_id)
        if acc_str not in mapping:
            mapping[acc_str] = acc_str
            
    return mapping
