import os
from contextlib import contextmanager
import pytest
from django.db import connection
from ledger.models import Tenant

@pytest.fixture(autouse=True, scope='session')
def require_runtime_role(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        with connection.cursor() as c:
            # Check if ledger_runtime exists
            c.execute("SELECT 1 FROM pg_roles WHERE rolname = 'ledger_runtime';")
            if not c.fetchone():
                pytest.fail("Role 'ledger_runtime' is missing. Please provision it before running tests.")


@contextmanager
def _as_owner():
    with connection.cursor() as c:
        c.execute("SELECT current_user <> session_user;")
        switched = c.fetchone()[0]
        role = None
        if switched:
            c.execute("SELECT current_user;")
            role = c.fetchone()[0]
            c.execute("RESET ROLE;")
    try:
        yield
    finally:
        if switched and role:
            with connection.cursor() as c:
                safe_role = role.replace('"', '""')
                c.execute(f'SET ROLE "{safe_role}";')


def create_tenant(*args, **kwargs):
    with _as_owner():
        return _orig_tenant_create(*args, **kwargs)

_orig_tenant_create = Tenant.objects.create
Tenant.objects.create = create_tenant

@pytest.fixture(autouse=True)
def close_replica_connection():
    # The replica alias is a test mirror with its own connection; close it so the test DB can be dropped.
    yield
    from django.db import connections
    connections['replica'].close()

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
    tenant = create_tenant(name='Managed Test Tenant')
    with _as_owner():
        account = Account.objects.create(tenant=tenant, name='Managed Test Account', type='ASSET', currency='USD')
    return tenant, account
