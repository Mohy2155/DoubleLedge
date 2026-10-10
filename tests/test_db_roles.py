import os
import pytest
import psycopg
import hashlib
import json
from decimal import Decimal
from django.db import connection, transaction
from django.db.utils import ProgrammingError
from django.conf import settings
from ledger.models import Tenant, Account, JournalEntry, TransactionLine
from ledger.services.posting import post_entry
from ledger.services.snapshots import BalanceQueryService
from tests.conftest import _as_owner, create_tenant

pytestmark = pytest.mark.runtime_role


def get_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).digest()


@pytest.mark.django_db
def test_rls_blocks_unscoped_queries_under_runtime_role(managed_tenant_and_account):
    tenant, account = managed_tenant_and_account
    assert Account.objects.count() == 0
    
    with transaction.atomic():
        with connection.cursor() as c:
            c.execute("SELECT set_config('app.tenant_id', %s, true);", [str(tenant.id)])
        assert Account.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_direct_connection_smoke_test(managed_tenant_and_account):
    tenant, account = managed_tenant_and_account
    db_settings = settings.DATABASES['default']
    
    if 'LEDGER_RUNTIME_PASSWORD' not in os.environ:
        pytest.skip("LEDGER_RUNTIME_PASSWORD env var not set; skipping direct connection test.")
    db_password = os.environ['LEDGER_RUNTIME_PASSWORD']
    
    with psycopg.connect(
        dbname=db_settings['NAME'],
        user='ledger_runtime',
        password=db_password,
        host=db_settings['HOST'],
        port=db_settings.get('PORT', '5432'),
        autocommit=True
    ) as conn:
        with conn.cursor() as c:
            c.execute("SELECT current_user;")
            assert c.fetchone()[0] == 'ledger_runtime'

            c.execute("SELECT count(*) FROM accounts;")
            assert c.fetchone()[0] == 0

            c.execute("SELECT set_config('app.tenant_id', %s, false);", [str(tenant.id)])
            c.execute("SELECT count(*) FROM accounts;")
            assert c.fetchone()[0] == 1


@pytest.mark.django_db
def test_post_entry_as_runtime(managed_tenant_and_account):
    tenant, account = managed_tenant_and_account
    
    with _as_owner():
        account2 = Account.objects.create(tenant=tenant, name='Acc 2', type='LIABILITY', currency='USD')

    with connection.cursor() as c:
        c.execute("SELECT current_user;")
        assert c.fetchone()[0] == 'ledger_runtime'
        
    with transaction.atomic():
        with connection.cursor() as c:
            c.execute("SELECT set_config('app.tenant_id', %s, true);", [str(tenant.id)])
        
        payload = {
            "reference_id": "TX_RUNTIME_1",
            "lines": [
                {"account_id": str(account.id), "amount": "150.00", "direction": "DEBIT"},
                {"account_id": str(account2.id), "amount": "150.00", "direction": "CREDIT"}
            ]
        }
        
        code, body, _ = post_entry(tenant, payload, "idem_RUNTIME_1", get_hash(payload))
        assert code == 201
        
        assert JournalEntry.objects.count() == 1
        assert TransactionLine.objects.count() == 2
        
        res1 = BalanceQueryService.get_account_balance(account)
        res2 = BalanceQueryService.get_account_balance(account2)
        assert res1['balance'] == Decimal('150.0000')
        assert res2['balance'] == Decimal('150.0000')


@pytest.mark.django_db
def test_post_entry_fails_without_tenant_context(managed_tenant_and_account):
    tenant, account = managed_tenant_and_account
    
    with _as_owner():
        account2 = Account.objects.create(tenant=tenant, name='Acc 3', type='LIABILITY', currency='USD')

    payload = {
        "reference_id": "TX_NO_CTX",
        "lines": [
            {"account_id": str(account.id), "amount": "50.00", "direction": "DEBIT"},
            {"account_id": str(account2.id), "amount": "50.00", "direction": "CREDIT"}
        ]
    }
    
    # Deliberately NOT setting app.tenant_id
    with pytest.raises(ProgrammingError, match="row-level security"):
        with transaction.atomic():
            post_entry(tenant, payload, "idem_NO_CTX", get_hash(payload))


@pytest.mark.django_db
def test_runtime_role_drops_bypassrls_and_superuser():
    with connection.cursor() as c:
        c.execute("""
            SELECT current_user, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb
            FROM pg_roles
            WHERE rolname = current_user;
        """)
        curr, is_super, is_bypass, is_createrole, is_createdb = c.fetchone()
        assert curr == 'ledger_runtime'
        assert is_super is False
        assert is_bypass is False
        assert is_createrole is False
        assert is_createdb is False

        # Role owns no objects in pg_class
        c.execute("""
            SELECT count(*)
            FROM pg_class c
            JOIN pg_roles r ON c.relowner = r.oid
            WHERE r.rolname = current_user;
        """)
        assert c.fetchone()[0] == 0

        # Role is not a member of any other role
        c.execute("""
            SELECT count(*)
            FROM pg_auth_members m
            JOIN pg_roles r ON m.member = r.oid
            WHERE r.rolname = current_user;
        """)
        assert c.fetchone()[0] == 0


@pytest.mark.django_db
def test_rls_under_plain_django_db_wrapper(managed_tenant_and_account):
    tenant, account = managed_tenant_and_account
    
    with connection.cursor() as c:
        c.execute("SELECT current_user;")
        assert c.fetchone()[0] == 'ledger_runtime'

    assert Account.objects.count() == 0

    with transaction.atomic():
        with connection.cursor() as c:
            c.execute("SELECT set_config('app.tenant_id', %s, true);", [str(tenant.id)])
        assert Account.objects.count() == 1


@pytest.mark.django_db
def test_runtime_cannot_mutate_ledger():
    with connection.cursor() as c:
        for t in ('journal_entries', 'transaction_lines'):
            for p in ('UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'):
                c.execute("SELECT has_table_privilege('ledger_runtime', %s, %s);", [t, p])
                assert c.fetchone()[0] is False, f"ledger_runtime should NOT have {p} on {t}"

        # Assert positive privilege on accounts
        c.execute("SELECT has_table_privilege('ledger_runtime', 'accounts', 'UPDATE');")
        assert c.fetchone()[0] is True, "ledger_runtime must have UPDATE on accounts for select_for_update"


@pytest.mark.django_db
def test_cross_tenant_isolation_under_runtime_role():
    tenant_a = create_tenant(name="Tenant A")
    tenant_b = create_tenant(name="Tenant B")

    with _as_owner():
        acc_a = Account.objects.create(tenant=tenant_a, name="Account A", type="ASSET", currency="USD")
        acc_b = Account.objects.create(tenant=tenant_b, name="Account B", type="ASSET", currency="USD")

    with transaction.atomic():
        with connection.cursor() as c:
            c.execute("SELECT set_config('app.tenant_id', %s, true);", [str(tenant_a.id)])

        # Tenant A context cannot see Tenant B's accounts
        assert Account.objects.filter(id=acc_a.id).exists()
        assert not Account.objects.filter(id=acc_b.id).exists()
        assert Account.objects.filter(tenant=tenant_b).count() == 0

        # Attempting to insert a row for Tenant B while in Tenant A context fails WITH CHECK violation
        with pytest.raises(ProgrammingError, match="row-level security"):
            with transaction.atomic():
                Account.objects.create(tenant=tenant_b, name="Illegal Cross-Tenant Acc", type="ASSET", currency="USD")
