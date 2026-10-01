import concurrent.futures
from decimal import Decimal

import pytest

from ledger.models import Account, Tenant
from ledger.services.posting import post_entry


@pytest.fixture
def tenant():
    return Tenant.objects.create(name="Deadlock Tenant")

@pytest.fixture
def accounts(tenant):
    a = Account.objects.create(
        tenant=tenant,
        name="Account A",
        type=Account.Type.ASSET,
        currency="USD",
        balance=Decimal("1000.00"),
        allow_negative=True
    )
    b = Account.objects.create(
        tenant=tenant,
        name="Account B",
        type=Account.Type.ASSET,
        currency="USD",
        balance=Decimal("1000.00"),
        allow_negative=True
    )
    return a, b


@pytest.mark.django_db(transaction=True)
def test_deadlock_prevention(tenant, accounts):
    a, b = accounts

    def worker(i):
        # Prevent threading DB connection sharing issues
        from django.db import connection
        connection.close()
        
        # Even threads do A->B, odd threads do B->A
        if i % 2 == 0:
            payload = {
                "reference_id": f"TX_{i}",
                "description": "A to B",
                "lines": [
                    {"account_id": str(a.id), "amount": "10.00", "direction": "CREDIT"},
                    {"account_id": str(b.id), "amount": "10.00", "direction": "DEBIT"}
                ]
            }
        else:
            payload = {
                "reference_id": f"TX_{i}",
                "description": "B to A",
                "lines": [
                    {"account_id": str(b.id), "amount": "10.00", "direction": "CREDIT"},
                    {"account_id": str(a.id), "amount": "10.00", "direction": "DEBIT"}
                ]
            }
        
        post_entry(tenant, payload, f"idem_{i}", f"hash_{i}".encode())
        return True

    # 10 concurrent transfers
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(worker, range(10)))
        
    assert all(results)
    
    a.refresh_from_db()
    b.refresh_from_db()
    
    # We did 5 transfers of $10 from A->B, and 5 from B->A
    # Net change is 0.
    assert a.balance == Decimal("1000.00")
    assert b.balance == Decimal("1000.00")
