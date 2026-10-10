import pytest
from django.db import connection
from ledger.models import Tenant
from ledger.services.tenancy import for_each_tenant, require_tenant_context
from workers.tasks import sweep_canary

pytestmark = pytest.mark.runtime_role


@pytest.mark.django_db
def test_list_tenant_ids_under_real_role_with_no_context():
    t1 = Tenant.objects.create(name="Tenant Alpha")
    t2 = Tenant.objects.create(name="Tenant Beta")
    
    with connection.cursor() as c:
        c.execute("SELECT current_user;")
        assert c.fetchone()[0] == 'ledger_runtime'
        
        c.execute("SELECT current_setting('app.tenant_id', true);")
        val = c.fetchone()[0]
        assert val is None or val == ''
        
        # B.1: SELECT count(*) FROM tenants returns 0
        c.execute("SELECT count(*) FROM tenants;")
        assert c.fetchone()[0] == 0
        
        # B.1: list_tenant_ids() returns both ids
        c.execute("SELECT id FROM public.list_tenant_ids();")
        listed_ids = {row[0] for row in c.fetchall()}
        assert t1.id in listed_ids
        assert t2.id in listed_ids

@pytest.mark.django_db
def test_for_each_tenant_returns_exact_stats(monkeypatch):
    t1 = Tenant.objects.create(name="Tenant Alpha")
    t2 = Tenant.objects.create(name="Tenant Beta")
    
    with connection.cursor() as c:
        c.execute("SELECT current_user;")
        assert c.fetchone()[0] == 'ledger_runtime'
        c.execute("SELECT current_setting('app.tenant_id', true);")
        val = c.fetchone()[0]
        assert val is None or val == ''

    # Ensure list_tenant_ids returns exactly the 2 tenants created here
    monkeypatch.setattr("ledger.services.tenancy.list_tenant_ids", lambda: [t1.id, t2.id])

    seen = []
    def record_tenant(tid):
        require_tenant_context()
        seen.append(tid)

    # B.2: for_each_tenant returns {listed: 2, processed: 2, failed: 0}
    stats = for_each_tenant(record_tenant)
    assert stats == {'listed': 2, 'processed': 2, 'failed': 0}
    assert set(seen) == {t1.id, t2.id}

@pytest.mark.django_db
def test_sweep_canary_reports_zero_tenants(monkeypatch):
    # Ensure listing returns zero
    monkeypatch.setattr("ledger.services.tenancy.list_tenant_ids", lambda: [])

    # B.3: If listing returns zero, the sweep reports it. Assert the real return value; don't print and pass.
    result = sweep_canary()
    assert result == {'alert': True, 'listed': 0, 'processed': 0, 'failed': 0}

@pytest.mark.django_db
def test_list_tenant_ids_not_granted_to_public():
    # B.4: EXECUTE on list_tenant_ids is not granted to PUBLIC. Show the ACL.
    with connection.cursor() as c:
        c.execute("""
            SELECT proacl::text[] 
            FROM pg_proc 
            WHERE proname = 'list_tenant_ids';
        """)
        row = c.fetchone()
        assert row is not None
        acl = row[0]
        
        # If granted to PUBLIC, there is an entry with an empty role name before '=', e.g. '=X/...'
        public_grants = [entry for entry in acl if entry.startswith("=")]
        assert len(public_grants) == 0, f"PUBLIC has grants in ACL: {acl}"
        
        # Verify ledger_runtime has EXECUTE
        runtime_grants = [entry for entry in acl if entry.startswith("ledger_runtime=")]
        assert len(runtime_grants) > 0, f"ledger_runtime missing in ACL: {acl}"


@pytest.mark.django_db
def test_require_tenant_context_fails_loudly():
    with pytest.raises(RuntimeError, match="No tenant context is set"):
        require_tenant_context()

