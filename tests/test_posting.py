from decimal import Decimal

import pytest

from ledger.models import Account, Tenant
from ledger.services.posting import post_entry


@pytest.fixture
def tenant():
    return Tenant.objects.create(name="Posting Tenant")

@pytest.fixture
def accounts(tenant):
    asset1 = Account.objects.create(
        tenant=tenant,
        name="Wallet 1",
        type=Account.Type.ASSET,
        currency="USD",
        balance=Decimal("1000.00"),
        allow_negative=False
    )
    asset2 = Account.objects.create(
        tenant=tenant,
        name="Wallet 2",
        type=Account.Type.ASSET,
        currency="USD",
        balance=Decimal("0.00"),
        allow_negative=False
    )
    return asset1, asset2

@pytest.mark.django_db(transaction=True)
def test_post_entry_updates_balances_correctly(tenant, accounts):
    a1, a2 = accounts
    
    payload = {
        "reference_id": "TX_001",
        "description": "Transfer",
        "lines": [
            {"account_id": str(a1.id), "amount": "100.00", "direction": "CREDIT"},
            {"account_id": str(a2.id), "amount": "100.00", "direction": "DEBIT"}
        ]
    }
    
    code, _resp, _ = post_entry(tenant, payload, "idem_001", b"hash_1")
    
    assert code == 201
    
    # Refresh accounts
    a1.refresh_from_db()
    a2.refresh_from_db()
    
    # Asset credit reduces balance, debit increases balance
    assert a1.balance == Decimal("900.00")
    assert a2.balance == Decimal("100.00")

from ledger.models import OutboxEvent

@pytest.mark.django_db(transaction=True)
def test_post_entry_creates_outbox_event(tenant, accounts):
    a1, a2 = accounts
    
    payload = {
        "reference_id": "TX_OUTBOX",
        "description": "Test outbox",
        "lines": [
            {"account_id": str(a1.id), "amount": "100.00", "direction": "CREDIT"},
            {"account_id": str(a2.id), "amount": "100.00", "direction": "DEBIT"}
        ]
    }
    
    assert OutboxEvent.objects.filter(tenant=tenant).count() == 0
    
    code, _resp, _ = post_entry(tenant, payload, "idem_outbox", b"hash_outbox")
    assert code == 201
    
    events = OutboxEvent.objects.filter(tenant=tenant)
    assert events.count() == 1
    
    event = events.first()
    assert event.type == "entry.posted"
    
    assert "lines" in event.payload
    for line in event.payload["lines"]:
        assert isinstance(line["amount"], str)
        assert not isinstance(line["amount"], (float, Decimal))

from unittest.mock import patch
from ledger.models import IdempotencyKey

@pytest.mark.django_db(transaction=True)
def test_post_entry_rolls_back_outbox_on_failure(tenant, accounts):
    a1, a2 = accounts
    payload = {
        "reference_id": "TX_ROLLBACK",
        "description": "Test rollback",
        "lines": [
            {"account_id": str(a1.id), "amount": "100.00", "direction": "CREDIT"},
            {"account_id": str(a2.id), "amount": "100.00", "direction": "DEBIT"}
        ]
    }
    
    assert OutboxEvent.objects.filter(tenant=tenant).count() == 0
    
    # We mock IdempotencyKey update to fail AFTER OutboxEvent is created
    with patch('django.db.models.query.QuerySet.update', side_effect=Exception("Simulated DB failure")):
        with pytest.raises(Exception, match="Simulated DB failure"):
            post_entry(tenant, payload, "idem_rb", b"hash_rb")
            
    # Verify rollback
    assert OutboxEvent.objects.filter(tenant=tenant).count() == 0

@pytest.mark.django_db(transaction=True)
def test_reversing_entry_emits_reversed_event(tenant, accounts):
    a1, a2 = accounts
    payload = {
        "reference_id": "TX_ORIGINAL",
        "lines": [
            {"account_id": str(a1.id), "amount": "100.00", "direction": "CREDIT"},
            {"account_id": str(a2.id), "amount": "100.00", "direction": "DEBIT"}
        ]
    }
    
    post_entry(tenant, payload, "idem_orig", b"hash_orig")
    
    # Now reverse it
    reverse_payload = {
        "reference_id": "TX_REV",
        "reverses_id": str(OutboxEvent.objects.first().payload["id"]),
        "lines": [
            {"account_id": str(a1.id), "amount": "100.00", "direction": "DEBIT"},
            {"account_id": str(a2.id), "amount": "100.00", "direction": "CREDIT"}
        ]
    }
    
    pre_count = OutboxEvent.objects.filter(tenant=tenant).count()
    post_entry(tenant, reverse_payload, "idem_rev", b"hash_rev")
    
    assert OutboxEvent.objects.filter(tenant=tenant).count() == pre_count + 1
    rev_event = OutboxEvent.objects.filter(tenant=tenant).order_by('-created_at').first()
    assert rev_event.type == "entry.reversed"
    
    # Idempotent replay
    post_entry(tenant, reverse_payload, "idem_rev", b"hash_rev")
    assert OutboxEvent.objects.filter(tenant=tenant).count() == pre_count + 1
