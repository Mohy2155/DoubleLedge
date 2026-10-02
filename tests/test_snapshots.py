import pytest
import threading
from decimal import Decimal
from django.utils import timezone
from ledger.models import Account, BalanceSnapshot, JournalEntry, TransactionLine, Tenant
from ledger.services.snapshots import generate_account_snapshot, BalanceQueryService
from workers.tasks import create_account_balance_snapshots
from unittest.mock import patch
from ledger.services.posting import post_entry
from freezegun import freeze_time
import uuid

@pytest.fixture
def tenant():
    return Tenant.objects.create(name="Snapshot Tenant")

@pytest.fixture
def accounts(tenant):
    asset1 = Account.objects.create(
        tenant=tenant,
        name="Wallet 1",
        type=Account.Type.ASSET,
        currency="USD",
        balance=Decimal("0.00"),
        allow_negative=True
    )
    asset2 = Account.objects.create(
        tenant=tenant,
        name="Wallet 2",
        type=Account.Type.ASSET,
        currency="USD",
        balance=Decimal("0.00"),
        allow_negative=True
    )
    return asset1, asset2

@pytest.mark.django_db(transaction=True)
def test_out_of_order_commit_concurrency(tenant, accounts):
    """
    Test that the snapshot worker safely handles out-of-order PostgreSQL commits 
    due to sequence generation by using a temporal buffer.
    """
    a1, a2 = accounts
    
    past_time = timezone.now() - timezone.timedelta(minutes=2)
    
    payload1 = {
        "reference_id": "TX_1",
        "lines": [
            {"account_id": str(a1.id), "amount": "100.00", "direction": "DEBIT"},
            {"account_id": str(a2.id), "amount": "100.00", "direction": "CREDIT"}
        ]
    }
    
    with freeze_time(past_time):
        post_entry(tenant, payload1, "idem_1", b"hash1")
    
    # We should have a balance of 100 on a1
    res = BalanceQueryService.get_account_balance(a1)
    assert res['balance'] == Decimal('100.0000')
    
    # Generate snapshot
    generate_account_snapshot(tenant.id, a1.id)
    
    snap1 = BalanceSnapshot.objects.filter(account=a1).order_by('-sequence').first()
    assert snap1 is not None
    assert snap1.balance == Decimal('100.0000')
    
    recent_time = timezone.now() - timezone.timedelta(seconds=10)
    
    payload2 = {
        "reference_id": "TX_2",
        "lines": [
            {"account_id": str(a1.id), "amount": "50.00", "direction": "DEBIT"},
            {"account_id": str(a2.id), "amount": "50.00", "direction": "CREDIT"}
        ]
    }
    
    with freeze_time(recent_time):
        post_entry(tenant, payload2, "idem_2", b"hash2")
    
    # Query should include the new lines
    res = BalanceQueryService.get_account_balance(a1)
    assert res['balance'] == Decimal('150.0000')
    
    # Generate snapshot again
    generate_account_snapshot(tenant.id, a1.id)
    
    # Snapshot should NOT have updated because TX_2 is within the 1-minute buffer
    snap2 = BalanceSnapshot.objects.filter(account=a1).order_by('-sequence').first()
    assert snap2.id == snap1.id
    assert snap2.balance == Decimal('100.0000')

@pytest.mark.django_db(transaction=True)
def test_polarity_inversion(tenant):
    """
    Verify that LIABILITY, EQUITY, and REVENUE accounts invert the normal balance equation
    (credit - debit) rather than (debit - credit) in both base snapshots and deltas.
    """
    liability_acc = Account.objects.create(
        tenant=tenant,
        name="Liability",
        type=Account.Type.LIABILITY,
        currency="USD",
        balance=Decimal("0.00"),
        allow_negative=True
    )
    asset_acc = Account.objects.create(
        tenant=tenant,
        name="Asset",
        type=Account.Type.ASSET,
        currency="USD",
        balance=Decimal("0.00"),
        allow_negative=True
    )

    past_time = timezone.now() - timezone.timedelta(minutes=2)
    with freeze_time(past_time):
        post_entry(tenant, {
            "reference_id": "L1",
            "lines": [
                {"account_id": str(asset_acc.id), "amount": "300.00", "direction": "DEBIT"},
                {"account_id": str(liability_acc.id), "amount": "300.00", "direction": "CREDIT"}
            ]
        }, "idem_L1", b"hash_L1")

    # Generate snapshot
    generate_account_snapshot(tenant.id, liability_acc.id)

    # Base snapshot balance for LIABILITY: credit(300) - debit(0) = 300
    snap = BalanceSnapshot.objects.filter(account=liability_acc).order_by('-sequence').first()
    assert snap is not None
    assert snap.balance == Decimal('300.0000')
    assert snap.credit_total == Decimal('300.0000')
    assert snap.debit_total == Decimal('0.0000')

    # Post an un-snapshotted delta
    recent_time = timezone.now() - timezone.timedelta(seconds=10)
    with freeze_time(recent_time):
        post_entry(tenant, {
            "reference_id": "L2",
            "lines": [
                {"account_id": str(liability_acc.id), "amount": "100.00", "direction": "DEBIT"},
                {"account_id": str(asset_acc.id), "amount": "100.00", "direction": "CREDIT"}
            ]
        }, "idem_L2", b"hash_L2")

    # The balance should now be 300 - 100 = 200
    res = BalanceQueryService.get_account_balance(liability_acc)
    assert res['balance'] == Decimal('200.0000')
    assert res['snapshot_used'] is True

@pytest.mark.django_db(transaction=True)
def test_snapshot_rls_isolation(tenant):
    """
    Ensure that a tenant cannot query or generate snapshots for an account belonging to another tenant.
    """
    tenant2 = Tenant.objects.create(name="Victim Tenant")
    victim_acc = Account.objects.create(
        tenant=tenant2,
        name="Victim Asset",
        type=Account.Type.ASSET,
        currency="USD",
        balance=Decimal("0.00"),
        allow_negative=True
    )
    
    # Use the malicious tenant context but request the victim account ID
    # get_account_balance uses `tenant_id = account.tenant_id`, so we must 
    # test raw SQL boundary via generating a snapshot with mismatching tenant_id/account_id
    
    generate_account_snapshot(tenant.id, victim_acc.id)
    
    # Should not have generated anything because l.tenant_id = %s filter prevents seeing lines
    assert BalanceSnapshot.objects.filter(account=victim_acc).count() == 0
    
    # Let's post something legitimately for victim
    past_time = timezone.now() - timezone.timedelta(minutes=2)
    with freeze_time(past_time):
        post_entry(tenant2, {
            "reference_id": "V1",
            "lines": [
                {"account_id": str(victim_acc.id), "amount": "100.00", "direction": "DEBIT"},
                {"account_id": str(victim_acc.id), "amount": "100.00", "direction": "CREDIT"} # dummy balance
            ]
        }, "idem_V1", b"hash_V1")
        
    generate_account_snapshot(tenant2.id, victim_acc.id)
    assert BalanceSnapshot.objects.filter(account=victim_acc).count() == 1
    
    # Attempting to read it via the DB using wrong tenant context should fail due to RLS
    # We can test RLS explicitly
    from django.db import connection
    with connection.cursor() as cursor:
        # Create restricted role for testing RLS
        cursor.execute("DROP ROLE IF EXISTS restricted_app_role;")
        cursor.execute("CREATE ROLE restricted_app_role LOGIN;")
        cursor.execute("GRANT SELECT ON balance_snapshots TO restricted_app_role;")
        
        # Switch to restricted role
        cursor.execute("SET ROLE restricted_app_role;")
        
        # Set wrong tenant context
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.id)])
        cursor.execute("SELECT COUNT(*) FROM balance_snapshots WHERE account_id = %s", [str(victim_acc.id)])
        count = cursor.fetchone()[0]
        
        cursor.execute("RESET ROLE;")
        
        assert count == 0, "RLS failed: Tenant 1 saw Tenant 2's snapshot"
