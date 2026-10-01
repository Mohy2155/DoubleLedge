import pytest

from ledger.models import Account, Tenant


@pytest.mark.django_db(transaction=True)
def test_models_can_query_existing_tables():
    tenant = Tenant.objects.create(name="Test Tenant")
    account = Account.objects.create(
        tenant=tenant,
        name="Test Account",
        type=Account.Type.ASSET,
        currency="USD",
        balance=0,
        allow_negative=True
    )
    
    assert Tenant.objects.count() == 1
    assert Account.objects.count() == 1
    assert Account.objects.first().id == account.id
