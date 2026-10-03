import pytest
import uuid
import json
import hashlib
from decimal import Decimal
import concurrent.futures

from django.db import connection, transaction
from rest_framework.test import APIClient

from ledger.models import Tenant, Account, JournalEntry
from ledger.services.sharding import create_sharded_account, get_aggregate_balance
from ledger.services.posting import post_entry, PostingError
from workers.tasks import rollup_shards

def get_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).digest()

@pytest.fixture
def edge_tenant():
    return Tenant.objects.create(name="Distributed Race Tenant")

@pytest.mark.django_db(transaction=True)
def test_sharded_account_strictness_mutually_exclusive(edge_tenant):
    from rest_framework.exceptions import ValidationError
    
    with pytest.raises(ValidationError) as excinfo:
        create_sharded_account(edge_tenant.id, "Race Master", "ASSET", "USD", allow_negative=False, shard_count=10)
        
    assert "mutually exclusive" in str(excinfo.value).lower()


@pytest.mark.django_db(transaction=True)
def test_deterministic_sweep_collision(edge_tenant):
    master_acc = create_sharded_account(edge_tenant.id, "Sweep Master", "REVENUE", "USD", allow_negative=True, shard_count=2)
    source_acc = create_sharded_account(edge_tenant.id, "Sweep Source", "ASSET", "USD", allow_negative=True, shard_count=0)
    
    # We want to manually seed Shard 0 with 50
    shards = list(Account.objects.filter(tenant_id=edge_tenant.id, parent_account_id=master_acc.id).order_by('id'))
    shard0 = shards[0]
    
    payload_seed0 = {
        'reference_id': 'sweep-seed-0',
        'lines': [
            {'account_id': str(source_acc.id), 'amount': '50.0000', 'direction': 'DEBIT'},
            {'account_id': str(shard0.id), 'amount': '50.0000', 'direction': 'CREDIT'}
        ]
    }
    post_entry(edge_tenant, payload_seed0, "idem-sweep-0", get_hash(payload_seed0), bypass_sharding=True)
    
    shard0.refresh_from_db()
    assert shard0.balance == Decimal('50.0000')
    master_acc.refresh_from_db()
    assert master_acc.balance == Decimal('0.0000')
    
    # Simulate a Celery worker double-execution by concurrently firing 5 threads
    def run_sweep(_):
        try:
            rollup_shards()
            return True
        except Exception as e:
            return str(e)
            
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(run_sweep, i) for i in range(5)]
        concurrent.futures.wait(futures)
        
    shard0.refresh_from_db()
    master_acc.refresh_from_db()
    
    if master_acc.balance > Decimal('50.0000'):
        pytest.fail(f"SWEEP COLLISION FLAW! Master balance is {master_acc.balance}, shard is {shard0.balance}")
        
    assert shard0.balance == Decimal('0.0000')
    assert master_acc.balance == Decimal('50.0000')
