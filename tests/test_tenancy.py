import pytest
from ledger.models import Tenant
from ledger.services.tenancy import for_each_tenant, require_tenant_context

@pytest.mark.django_db(transaction=True)
def test_for_each_tenant():
    t1 = Tenant.objects.create(name="T1")
    t2 = Tenant.objects.create(name="T2")
    
    seen = []
    def my_func(tenant_id):
        require_tenant_context()
        seen.append(tenant_id)
        
    for_each_tenant(my_func)
    
    assert t1.id in seen
    assert t2.id in seen

@pytest.mark.django_db
def test_require_tenant_context_fails_loudly():
    with pytest.raises(RuntimeError, match="No tenant context is set"):
        require_tenant_context()
