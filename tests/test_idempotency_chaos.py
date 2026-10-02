import concurrent.futures
from decimal import Decimal

import pytest
from django.db import connection

from ledger.models import Account, IdempotencyKey, JournalEntry, Tenant
from ledger.services.posting import PostingError, post_entry


@pytest.fixture
def tenant():
    return Tenant.objects.create(name="Chaos Tenant")

@pytest.fixture
def accounts(tenant):
    a = Account.objects.create(
        tenant=tenant, name="Account A", type=Account.Type.ASSET, currency="USD", balance=Decimal("1000.00"), allow_negative=True
    )
    b = Account.objects.create(
        tenant=tenant, name="Account B", type=Account.Type.ASSET, currency="USD", balance=Decimal("0.00"), allow_negative=True
    )
    return a, b

@pytest.mark.django_db(transaction=True)
def test_concurrent_identical_requests(tenant, accounts):
    a, b = accounts
    payload = {
        "reference_id": "CH_001",
        "description": "Concurrent Test",
        "lines": [
            {"account_id": str(a.id), "amount": "50.00", "direction": "CREDIT"},
            {"account_id": str(b.id), "amount": "50.00", "direction": "DEBIT"}
        ]
    }
    
    def worker():
        connection.close()
        code, _resp, _ = post_entry(tenant, payload, "chaos_idem_1", b"req_hash_1")
        return code
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(lambda _: worker(), range(20)))
        
    assert all(code == 201 for code in results)
    
    assert JournalEntry.objects.filter(tenant=tenant).count() == 1
    assert IdempotencyKey.objects.filter(tenant=tenant).count() == 1
    
    a.refresh_from_db()
    b.refresh_from_db()
    
    assert a.balance == Decimal("950.00")
    assert b.balance == Decimal("50.00")

@pytest.mark.django_db(transaction=True)
def test_payload_conflict_on_same_key(tenant, accounts):
    a, b = accounts
    payload_a = {
        "reference_id": "CH_002",
        "description": "Conflict A",
        "lines": [
            {"account_id": str(a.id), "amount": "10.00", "direction": "CREDIT"},
            {"account_id": str(b.id), "amount": "10.00", "direction": "DEBIT"}
        ]
    }
    payload_b = {
        "reference_id": "CH_003",
        "description": "Conflict B",
        "lines": [
            {"account_id": str(a.id), "amount": "20.00", "direction": "CREDIT"},
            {"account_id": str(b.id), "amount": "20.00", "direction": "DEBIT"}
        ]
    }
    
    code, _resp, _ = post_entry(tenant, payload_a, "conflict_key", b"hash_A")
    assert code == 201
    
    with pytest.raises(PostingError) as exc:
        post_entry(tenant, payload_b, "conflict_key", b"hash_B")
        
    assert "idempotency_key_reuse" in str(exc.value)

@pytest.mark.django_db(transaction=True)
def test_transaction_abort_crash_rollback(tenant, accounts, monkeypatch):
    a, b = accounts
    payload = {
        "reference_id": "CH_004",
        "description": "Crash",
        "lines": [
            {"account_id": str(a.id), "amount": "10.00", "direction": "CREDIT"},
            {"account_id": str(b.id), "amount": "10.00", "direction": "DEBIT"}
        ]
    }
    
    # Simulate a crash right after checking idempotency by patching `Account.objects.select_for_update`
    
    def mocked_select_for_update(*args, **kwargs):
        raise RuntimeError("Simulated crash")
        
    monkeypatch.setattr("ledger.models.Account.objects.select_for_update", mocked_select_for_update)
    
    with pytest.raises(RuntimeError):
        post_entry(tenant, payload, "crash_key", b"crash_hash")
        
    # Transaction should have rolled back entirely
    assert IdempotencyKey.objects.filter(tenant=tenant, key="crash_key").count() == 0
    
    # Now restore and verify it succeeds
    monkeypatch.undo()
    code, _resp, _ = post_entry(tenant, payload, "crash_key", b"crash_hash")
    assert code == 201
    assert IdempotencyKey.objects.filter(tenant=tenant, key="crash_key").count() == 1
