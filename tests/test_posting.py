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
    
    code, resp = post_entry(tenant, payload, "idem_001", b"hash_1")
    
    assert code == 201
    
    # Refresh accounts
    a1.refresh_from_db()
    a2.refresh_from_db()
    
    # Asset credit reduces balance, debit increases balance
    assert a1.balance == Decimal("900.00")
    assert a2.balance == Decimal("100.00")
