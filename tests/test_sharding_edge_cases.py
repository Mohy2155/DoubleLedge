import pytest
import uuid
import json
import hashlib
from decimal import Decimal
from unittest import mock

from django.db import connection, transaction, IntegrityError
from django.utils import timezone

from ledger.models import Tenant, Account, TransactionLine
from ledger.services.sharding import create_sharded_account, get_aggregate_balance
from ledger.services.posting import post_entry, PostingError

def get_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).digest()

@pytest.fixture
def edge_tenant():
    return Tenant.objects.create(name="Edge Case Tenant")



@pytest.mark.django_db(transaction=True)
def test_statement_cursor_drift_during_rollup(edge_tenant):
    from django.test import RequestFactory
    from api.views import AccountViewSet
    
    master_acc = create_sharded_account(edge_tenant.id, "Drift Acc", "REVENUE", "USD", allow_negative=True, shard_count=2)
    source_acc = create_sharded_account(edge_tenant.id, "Drift Source", "ASSET", "USD", allow_negative=True, shard_count=0)
    
    # Post 5 entries
    for i in range(5):
        payload = {
            'reference_id': f'drift-tx-{i}',
            'lines': [
                {'account_id': str(source_acc.id), 'amount': '10.0000', 'direction': 'DEBIT'},
                {'account_id': str(master_acc.id), 'amount': '10.0000', 'direction': 'CREDIT'}
            ]
        }
        post_entry(edge_tenant, payload, f"idem-drift-{i}", get_hash(payload))
        
    from rest_framework.test import APIClient
    client = APIClient()
    
    from api.authentication import TenantUser
    user = TenantUser(edge_tenant.id)
    client.force_authenticate(user=user)

    # Fetch Page 1 (limit 2)
    response1 = client.get(f'/api/v1/accounts/{master_acc.id}/statement/?limit=2')
    
    assert response1.status_code == 200
    data1 = response1.data
    assert len(data1['results']) == 2
    cursor = data1['next_cursor']
    
    # Run rollup
    from workers.tasks import rollup_shards
    rollup_shards()
    
    # Fetch Page 2
    response2 = client.get(f'/api/v1/accounts/{master_acc.id}/statement/?limit=10&cursor={cursor}')
    
    assert response2.status_code == 200
    data2 = response2.data
    
    # The rollup adds 2 internal lines, but they are excluded from the statement.
    # We should get exactly the 3 remaining original lines.
    assert len(data2['results']) == 3
    
    # Check final running balance
    final_balance = Decimal(data2['results'][-1]['running_balance'])
    
    aggs = get_aggregate_balance(edge_tenant.id, master_acc.id)
    assert final_balance == aggs['posted_balance']

@pytest.mark.django_db(transaction=True)
def test_statement_identical_before_and_after_rollup(edge_tenant):
    master_acc = create_sharded_account(edge_tenant.id, "Full Acc", "REVENUE", "USD", allow_negative=True, shard_count=2)
    source_acc = create_sharded_account(edge_tenant.id, "Full Source", "ASSET", "USD", allow_negative=True, shard_count=0)
    
    for i in range(5):
        payload = {
            'reference_id': f'full-tx-{i}',
            'lines': [
                {'account_id': str(source_acc.id), 'amount': '10.0000', 'direction': 'DEBIT'},
                {'account_id': str(master_acc.id), 'amount': '10.0000', 'direction': 'CREDIT'}
            ]
        }
        post_entry(edge_tenant, payload, f"idem-full-{i}", get_hash(payload))
        
    from rest_framework.test import APIClient
    from api.authentication import TenantUser
    client = APIClient()
    client.force_authenticate(user=TenantUser(edge_tenant.id))
    
    res_before = client.get(f'/api/v1/accounts/{master_acc.id}/statement/?limit=100')
    assert res_before.status_code == 200
    statement_before = res_before.data['results']
    
    from workers.tasks import rollup_shards
    rollup_shards()
    
    res_after = client.get(f'/api/v1/accounts/{master_acc.id}/statement/?limit=100')
    assert res_after.status_code == 200
    statement_after = res_after.data['results']
    
    assert statement_before == statement_after

@pytest.mark.django_db(transaction=True)
def test_rollup_creates_zero_outbox_events(edge_tenant):
    master_acc = create_sharded_account(edge_tenant.id, "Outbox Acc", "REVENUE", "USD", allow_negative=True, shard_count=2)
    source_acc = create_sharded_account(edge_tenant.id, "Outbox Source", "ASSET", "USD", allow_negative=True, shard_count=0)
    
    payload = {
        'reference_id': 'outbox-tx-1',
        'lines': [
            {'account_id': str(source_acc.id), 'amount': '10.0000', 'direction': 'DEBIT'},
            {'account_id': str(master_acc.id), 'amount': '10.0000', 'direction': 'CREDIT'}
        ]
    }
    post_entry(edge_tenant, payload, "idem-outbox-1", get_hash(payload))
    
    from ledger.models import OutboxEvent
    outbox_count_before = OutboxEvent.objects.count()
    
    from workers.tasks import rollup_shards
    rollup_shards()
    
    outbox_count_after = OutboxEvent.objects.count()
    assert outbox_count_after == outbox_count_before
