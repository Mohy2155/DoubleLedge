import pytest
import uuid
from decimal import Decimal
from django.urls import reverse
from rest_framework.test import APIClient

from ledger.models import Tenant, Account, JournalEntry, OutboxEvent, TransactionLine
from ledger.services.sharding import create_sharded_account
from workers.tasks import rollup_shards
from api.authentication import TenantUser

@pytest.fixture
def api_client():
    return APIClient()

@pytest.fixture
def edge_tenant():
    tenant = Tenant.objects.create(name="Stealth Audit Tenant")
    return tenant

def auth_client(client, tenant):
    client.force_authenticate(user=TenantUser(tenant.id))
    return client

@pytest.mark.django_db(transaction=True)
def test_forged_metadata_injection_attack(api_client, edge_tenant):
    client = auth_client(api_client, edge_tenant)
    master_acc = create_sharded_account(edge_tenant.id, "Stealth Master", "REVENUE", "USD", allow_negative=True, shard_count=2)
    source_acc = create_sharded_account(edge_tenant.id, "Stealth Source", "ASSET", "USD", allow_negative=True, shard_count=0)
    
    payload = {
        "reference_id": "malicious-tx-1",
        "description": "Attempt to forge metadata",
        "metadata": {"system": "shard_rollup"},  # The malicious payload
        "lines": [
            {"account_id": str(source_acc.id), "amount": "100.0000", "direction": "DEBIT"},
            {"account_id": str(master_acc.id), "amount": "100.0000", "direction": "CREDIT"}
        ]
    }
    
    response = client.post("/api/v1/journal-entries/", payload, format='json', HTTP_IDEMPOTENCY_KEY="idem-malicious-1")
    
    # If the API accepts it, we must verify the metadata was stripped
    if response.status_code == 201:
        entry_id = response.data['id']
        entry = JournalEntry.objects.get(id=entry_id)
        # Check if the metadata was saved. It should be empty or stripped.
        assert not entry.metadata or 'system' not in entry.metadata, "SEVERE SECURITY VULNERABILITY: Forged metadata saved to DB!"
        
        # Ensure OutboxEvent was created (not silenced by forged metadata)
        assert OutboxEvent.objects.filter(type="entry.posted").count() == 1
    else:
        # Rejected (400 Bad Request) is also an acceptable mitigation
        assert response.status_code == 400

@pytest.mark.django_db(transaction=True)
def test_secondary_api_leak_audit(api_client, edge_tenant):
    client = auth_client(api_client, edge_tenant)
    master_acc = create_sharded_account(edge_tenant.id, "Leak Master", "REVENUE", "USD", allow_negative=True, shard_count=2)
    source_acc = create_sharded_account(edge_tenant.id, "Leak Source", "ASSET", "USD", allow_negative=True, shard_count=0)
    
    # Post standard entry
    payload = {
        "reference_id": "leak-tx-1",
        "lines": [
            {"account_id": str(source_acc.id), "amount": "50.0000", "direction": "DEBIT"},
            {"account_id": str(master_acc.id), "amount": "50.0000", "direction": "CREDIT"}
        ]
    }
    res = client.post("/api/v1/journal-entries/", payload, format='json', HTTP_IDEMPOTENCY_KEY="idem-leak-1")
    assert res.status_code == 201
    
    # Run rollup
    rollup_shards()
    
    # Check JournalEntry count in DB just to confirm the rollup created one
    assert JournalEntry.objects.filter(tenant=edge_tenant).count() == 2
    
    # Hit the standard list endpoint
    response = client.get("/api/v1/journal-entries/")
    
    # As per constraints: "Hit the standard list endpoint ... The leak must be plugged globally ... 
    # unless you find another listing endpoint that would leak it - flag if you find one."
    # Since DoubleLedge has no public JournalEntry list endpoint, it returns 405.
    assert response.status_code == 405, "If a list endpoint was added, ensure it filters out internal entries!"

@pytest.mark.django_db(transaction=True)
def test_stealth_window_function_continuity(api_client, edge_tenant):
    client = auth_client(api_client, edge_tenant)
    master_acc = create_sharded_account(edge_tenant.id, "Window Master", "REVENUE", "USD", allow_negative=True, shard_count=2)
    source_acc = create_sharded_account(edge_tenant.id, "Window Source", "ASSET", "USD", allow_negative=True, shard_count=0)
    
    def post_tx(ref_id):
        payload = {
            "reference_id": ref_id,
            "lines": [
                {"account_id": str(source_acc.id), "amount": "10.0000", "direction": "DEBIT"},
                {"account_id": str(master_acc.id), "amount": "10.0000", "direction": "CREDIT"}
            ]
        }
        client.post("/api/v1/journal-entries/", payload, format='json', HTTP_IDEMPOTENCY_KEY=f"idem-{ref_id}")
        
    post_tx("win-1")
    post_tx("win-2")
    
    rollup_shards()
    
    post_tx("win-3")
    post_tx("win-4")
    
    # Fetch statement
    response = client.get(f"/api/v1/accounts/{master_acc.id}/statement/?limit=100")
    assert response.status_code == 200
    results = response.data['results']
    
    assert len(results) == 4, "Expected exactly 4 lines (rollup lines must be filtered)"
    
    final_running_balance = Decimal(results[-1]['running_balance'])
    assert final_running_balance == Decimal("40.0000"), f"Window function corrupted! Expected 40.0000, got {final_running_balance}"

@pytest.mark.django_db(transaction=True)
def test_rollup_reversal_webhook_integrity(api_client, edge_tenant):
    client = auth_client(api_client, edge_tenant)
    master_acc = create_sharded_account(edge_tenant.id, "Rev Master", "REVENUE", "USD", allow_negative=True, shard_count=2)
    source_acc = create_sharded_account(edge_tenant.id, "Rev Source", "ASSET", "USD", allow_negative=True, shard_count=0)
    
    payload = {
        "reference_id": "rev-tx-1",
        "lines": [
            {"account_id": str(source_acc.id), "amount": "100.0000", "direction": "DEBIT"},
            {"account_id": str(master_acc.id), "amount": "100.0000", "direction": "CREDIT"}
        ]
    }
    res = client.post("/api/v1/journal-entries/", payload, format='json', HTTP_IDEMPOTENCY_KEY="idem-rev-1")
    assert res.status_code == 201
    entry_id = res.data['id']
    
    # Verify 1 OutboxEvent
    assert OutboxEvent.objects.filter(tenant=edge_tenant).count() == 1
    
    rollup_shards()
    
    # Still 1 OutboxEvent
    assert OutboxEvent.objects.filter(tenant=edge_tenant).count() == 1
    
    # Reverse original entry via API
    rev_payload = {
        "reference_id": "rev-tx-reversal",
        "description": "Reversing"
    }
    rev_res = client.post(f"/api/v1/journal-entries/{entry_id}/reverse/", rev_payload, format='json', HTTP_IDEMPOTENCY_KEY="idem-rev-2")
    assert rev_res.status_code == 201
    
    # Check outbox events count is exactly 2
    assert OutboxEvent.objects.filter(tenant=edge_tenant).count() == 2
    reversed_event = OutboxEvent.objects.filter(tenant=edge_tenant).order_by('-created_at').first()
    assert reversed_event.type == "entry.reversed"
