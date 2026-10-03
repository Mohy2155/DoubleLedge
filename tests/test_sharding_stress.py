import pytest
import uuid
import json
import hashlib
from decimal import Decimal
import threading
from concurrent.futures import ThreadPoolExecutor

from django.db import connection, connections
from django.utils import timezone

from ledger.models import Tenant, Account, OutboxEvent, TransactionLine
from ledger.services.sharding import create_sharded_account, get_aggregate_balance, resolve_member_ids
from ledger.services.posting import post_entry

def get_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).digest()

def close_connections():
    for conn in connections.all():
        conn.close()

@pytest.fixture
def stress_tenant():
    return Tenant.objects.create(name="Sharding Stress Tenant")

def make_request(tenant, target_master_id, source_id, idx, amount='1.0000'):
    close_connections()
    try:
        payload = {
            'reference_id': f'thundering-herd-{idx}-{uuid.uuid4()}',
            'lines': [
                {
                    'account_id': str(source_id),
                    'amount': amount,
                    'direction': 'DEBIT'
                },
                {
                    'account_id': str(target_master_id),
                    'amount': amount,
                    'direction': 'CREDIT'
                }
            ]
        }
        req_hash = get_hash(payload)
        from ledger.services.posting import post_entry
        post_entry(tenant, payload, f"idem-{payload['reference_id']}", req_hash)
        return "SUCCESS", None
    except Exception as e:
        return "ERROR", str(e)
    finally:
        close_connections()

def make_rollup(idx):
    close_connections()
    try:
        from workers.tasks import rollup_shards
        rollup_shards()
        return "SUCCESS", None
    except Exception as e:
        return "ERROR", str(e)
    finally:
        close_connections()

@pytest.mark.django_db(transaction=True)
def test_sharding_throughput_and_distribution(stress_tenant):
    master_acc = create_sharded_account(stress_tenant.id, "High Volume", "REVENUE", "USD", allow_negative=True, shard_count=10)
    source_acc = create_sharded_account(stress_tenant.id, "Source", "ASSET", "USD", allow_negative=True, shard_count=0)
    
    num_threads = 100
    successes = 0
    errors = []
    
    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = [
            executor.submit(make_request, stress_tenant, master_acc.id, source_acc.id, i)
            for i in range(num_threads)
        ]
        
        for f in futures:
            res_type, val = f.result()
            if res_type == "SUCCESS":
                successes += 1
            else:
                errors.append(val)
                
    assert successes == 100, f"Expected 100 successes, got {successes}. Errors: {errors}"
    
    aggs = get_aggregate_balance(stress_tenant.id, master_acc.id)
    assert aggs['posted_balance'] == Decimal('100.0000')
    
    # Verify via DB that balance is distributed
    shards = Account.objects.filter(tenant_id=stress_tenant.id, parent_account_id=master_acc.id)
    shard_balances = [s.balance for s in shards]
    assert sum(shard_balances) == Decimal('100.0000')
    
    master_acc.refresh_from_db()
    assert master_acc.balance == Decimal('0.0000')  # Since no rollup has occurred

@pytest.mark.django_db(transaction=True)
def test_live_postings_vs_rollup_contention(stress_tenant):
    master_acc = create_sharded_account(stress_tenant.id, "High Volume 2", "REVENUE", "USD", allow_negative=True, shard_count=5)
    source_acc = create_sharded_account(stress_tenant.id, "Source 2", "ASSET", "USD", allow_negative=True, shard_count=0)
    
    num_posting_threads = 50
    num_rollup_threads = 10
    
    with ThreadPoolExecutor(max_workers=30) as executor:
        post_futures = [
            executor.submit(make_request, stress_tenant, master_acc.id, source_acc.id, i)
            for i in range(num_posting_threads)
        ]
        rollup_futures = [
            executor.submit(make_rollup, i)
            for i in range(num_rollup_threads)
        ]
        
        post_successes = 0
        post_errors = []
        for f in post_futures:
            res_type, val = f.result()
            if res_type == "SUCCESS":
                post_successes += 1
            else:
                post_errors.append(val)
                
        rollup_successes = 0
        rollup_errors = []
        for f in rollup_futures:
            res_type, val = f.result()
            if res_type == "SUCCESS":
                rollup_successes += 1
            else:
                rollup_errors.append(val)

    assert post_successes == 50, f"Expected 50 posting successes, got {post_successes}. Errors: {post_errors}"
    
    aggs = get_aggregate_balance(stress_tenant.id, master_acc.id)
    assert aggs['posted_balance'] == Decimal('50.0000')
    
    # Run a final rollup to ensure everything is settled
    from workers.tasks import rollup_shards
    rollup_shards()
    
    # All balances should now be in the master account
    master_acc.refresh_from_db()
    assert master_acc.balance == Decimal('50.0000')
    
    shards = Account.objects.filter(tenant_id=stress_tenant.id, parent_account_id=master_acc.id)
    for shard in shards:
        assert shard.balance == Decimal('0.0000')


@pytest.mark.django_db(transaction=True)
def test_reversal_determinism_and_id_leak_audit(stress_tenant):
    master_acc = create_sharded_account(stress_tenant.id, "Leak Test", "REVENUE", "USD", allow_negative=True, shard_count=2)
    source_acc = create_sharded_account(stress_tenant.id, "Leak Source", "ASSET", "USD", allow_negative=True, shard_count=0)
    
    payload = {
        'reference_id': 'original_leak_test',
        'lines': [
            {'account_id': str(source_acc.id), 'amount': '25.0000', 'direction': 'DEBIT'},
            {'account_id': str(master_acc.id), 'amount': '25.0000', 'direction': 'CREDIT'}
        ]
    }
    idem = "idem_leak_test"
    req_hash = get_hash(payload)
    
    code, body, _ = post_entry(stress_tenant, payload, idem, req_hash)
    assert code == 201
    
    # ID Leak Audit: ensure physical shard ID is NOT in the API response
    lines_body = body['lines']
    found_master_id = False
    for line in lines_body:
        acc_id = line['account_id']
        assert acc_id in (str(master_acc.id), str(source_acc.id))
        if acc_id == str(master_acc.id):
            found_master_id = True
    assert found_master_id
    
    # Check outbox event payload
    event = OutboxEvent.objects.filter(tenant=stress_tenant, type="entry.posted", payload__reference_id="original_leak_test").first()
    found_master_id_in_outbox = False
    for line in event.payload['lines']:
        acc_id = line['account_id']
        assert acc_id in (str(master_acc.id), str(source_acc.id))
        if acc_id == str(master_acc.id):
            found_master_id_in_outbox = True
    assert found_master_id_in_outbox
    
    # Now verify via raw SQL/DB that it actually routed to a physical shard
    original_entry_id = event.payload['id']
    shard_lines = TransactionLine.objects.filter(
        journal_entry_id=original_entry_id, 
        account__parent_account_id=master_acc.id
    )
    assert shard_lines.count() == 1
    actual_shard_id = shard_lines.first().account_id
    assert actual_shard_id != master_acc.id
    
    # Reverse the entry (simulate how the API does it by reading the physical lines)
    original_lines = TransactionLine.objects.filter(journal_entry_id=original_entry_id)
    rev_lines = []
    for line in original_lines:
        rev_lines.append({
            'account_id': str(line.account_id),
            'amount': str(line.amount),
            'direction': 'CREDIT' if line.direction == 'DEBIT' else 'DEBIT'
        })
        
    reverse_payload = {
        'reference_id': 'reversal_leak_test',
        'reverses_id': original_entry_id,
        'lines': rev_lines
    }
    idem_rev = "idem_rev_leak_test"
    req_hash_rev = get_hash(reverse_payload)
    
    code_rev, body_rev, _ = post_entry(stress_tenant, reverse_payload, idem_rev, req_hash_rev)
    assert code_rev == 201
    
    reversal_entry_id = body_rev['id']
    rev_shard_lines = TransactionLine.objects.filter(
        journal_entry_id=reversal_entry_id,
        account__parent_account_id=master_acc.id
    )
    assert rev_shard_lines.count() == 1
    rev_shard_id = rev_shard_lines.first().account_id
    
    # Crucial assertion: Reversal routed to the exact same physical shard
    assert rev_shard_id == actual_shard_id
