import pytest
from decimal import Decimal
from django.db import connection
from ledger.models import Tenant, Account, ReconciliationReport
from ledger.services.reconciliation import check_tenant_ledger_integrity
from ledger.services.posting import post_entry

@pytest.mark.django_db
def test_reconciliation_detects_drift():
    # 1. Setup tenant and accounts
    tenant = Tenant.objects.create(name="Test Tenant")
    
    asset = Account.objects.create(
        tenant=tenant, 
        name="Cash", 
        type=Account.Type.ASSET, 
        currency="USD", 
        balance=Decimal("0.0000")
    )
    revenue = Account.objects.create(
        tenant=tenant, 
        name="Sales", 
        type=Account.Type.REVENUE, 
        currency="USD", 
        balance=Decimal("0.0000")
    )

    # 2. Post a valid entry to give them some balance
    post_entry(
        tenant=tenant, 
        payload={
            "reference_id": "entry-1", 
            "description": "Test Sale", 
            "lines": [
                {"account_id": str(asset.id), "direction": "DEBIT", "amount": "100.0000"},
                {"account_id": str(revenue.id), "direction": "CREDIT", "amount": "100.0000"},
            ]
        }, 
        idempotency_key="key-1", 
        request_hash=b"hash"
    )

    # Ensure it's clean first
    report = check_tenant_ledger_integrity(tenant.id)
    assert report.status == ReconciliationReport.Status.CLEAN
    assert report.accounts_checked == 2
    assert len(report.discrepancies) == 0

    # 3. Simulate out-of-band balance mutation (drift)
    # We must do this via raw SQL or ORM update without creating transaction lines.
    # We'll use ORM update which bypasses the ledger logic but mutates the table.
    Account.objects.filter(id=asset.id).update(balance=Decimal("150.0000"))

    # 4. Run reconciliation
    report2 = check_tenant_ledger_integrity(tenant.id)
    
    # 5. Assertions
    assert report2.status == ReconciliationReport.Status.DRIFT_DETECTED
    assert report2.accounts_checked == 2
    assert len(report2.discrepancies) == 1
    
    discrepancy = report2.discrepancies[0]
    assert discrepancy["account_id"] == str(asset.id)
    assert discrepancy["expected_balance"] == "150.0000"
    assert discrepancy["actual_balance"] == "100.0000"
    assert discrepancy["difference"] == "-50.0000"
    
    # Verify no TypeErrors from JSON serialization
    assert type(discrepancy["expected_balance"]) == str
    assert type(discrepancy["actual_balance"]) == str
