import os
import pytest
from django.db import connection
from ledger.models import Tenant

@pytest.fixture(autouse=True, scope='session')
def apply_test_db_grants(django_db_setup, django_db_blocker):
    if 'LEDGER_RUNTIME_PASSWORD' not in os.environ:
        pytest.fail("LEDGER_RUNTIME_PASSWORD env var must be set for the test suite.")
    
    with django_db_blocker.unblock():
        with connection.cursor() as c:
            # Check if ledger_runtime exists
            c.execute("SELECT 1 FROM pg_roles WHERE rolname = 'ledger_runtime';")
            if not c.fetchone():
                pytest.fail("Role 'ledger_runtime' is missing. Please provision it before running tests.")


def create_tenant(*args, **kwargs):
    with connection.cursor() as c:
        c.execute("SELECT current_user;")
        curr = c.fetchone()[0]
        if curr != 'postgres':
            c.execute('RESET ROLE;')
            need_restore = True
        else:
            need_restore = False
    try:
        return _orig_tenant_create(*args, **kwargs)
    finally:
        if need_restore:
            with connection.cursor() as c:
                c.execute(f"SET ROLE {curr};")

_orig_tenant_create = Tenant.objects.create
Tenant.objects.create = create_tenant

@pytest.fixture
def tenant_creator():
    return create_tenant

@pytest.fixture(autouse=True)
def switch_to_runtime_role(request, db):
    if request.node.get_closest_marker("runtime_role"):
        with connection.cursor() as c:
            c.execute('SET ROLE ledger_runtime;')
        try:
            yield
        finally:
            with connection.cursor() as c:
                c.execute('RESET ROLE;')
    else:
        yield

@pytest.fixture
def managed_tenant_and_account(db):
    from ledger.models import Account
    tenant = create_tenant(name='Spike Tenant 2')
    with connection.cursor() as c:
        c.execute('RESET ROLE;')
    try:
        account = Account.objects.create(tenant=tenant, name='Spike Acc 2', type='ASSET', currency='USD')
        return tenant, account
    finally:
        with connection.cursor() as c:
            c.execute('SET ROLE ledger_runtime;')

