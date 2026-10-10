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

pytestmark = pytest.mark.runtime_role


def get_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).digest()

@pytest.mark.django_db(transaction=True)
def test_rls_blocks_unscoped_queries_under_runtime_role(managed_tenant_and_account):
    tenant, account = managed_tenant_and_account
    assert Account.objects.count() == 0
    
    with transaction.atomic():
        with connection.cursor() as c:
            c.execute(f"SELECT set_config('app.tenant_id', '{tenant.id}', true);")
        assert Account.objects.count() == 1

@pytest.mark.django_db(transaction=True)
def test_teardown_and_flush_cycle(managed_tenant_and_account):
    pass

@pytest.mark.django_db(transaction=True)
def test_direct_connection_smoke_test(managed_tenant_and_account):
    tenant, account = managed_tenant_and_account
    db_settings = settings.DATABASES['default']
    
    if 'LEDGER_RUNTIME_PASSWORD' not in os.environ:
        pytest.skip("LEDGER_RUNTIME_PASSWORD env var not set; skipping direct connection test.")
    db_password = os.environ['LEDGER_RUNTIME_PASSWORD']
    
    conn = psycopg.connect(
        dbname=db_settings['NAME'], user='ledger_runtime', password=db_password,
        host=db_settings.get('HOST', 'db'), port=db_settings.get('PORT', '5432')
    )
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute('SELECT count(*) FROM accounts;')
        assert c.fetchone()[0] == 0
        c.execute(f"SELECT set_config('app.tenant_id', '{tenant.id}', false);")
        c.execute('SELECT count(*) FROM accounts;')
        assert c.fetchone()[0] == 1
    conn.close()

@pytest.mark.django_db(transaction=True)
def test_post_entry_as_runtime(managed_tenant_and_account):
    tenant, account = managed_tenant_and_account
    
    with connection.cursor() as c:
        c.execute('RESET ROLE;')
    account2 = Account.objects.create(tenant=tenant, name='Acc 3', type='LIABILITY', currency='USD')
    with connection.cursor() as c:
        c.execute('SET ROLE ledger_runtime;')

    with connection.cursor() as c:
        c.execute('SELECT current_user;')
        assert c.fetchone()[0] == 'ledger_runtime'
        
    with transaction.atomic():
        with connection.cursor() as c:
            c.execute(f"SELECT set_config('app.tenant_id', '{tenant.id}', true);")
        
        payload = {
            "reference_id": "TX_SPK1",
            "lines": [
                {"account_id": str(account.id), "amount": "150.00", "direction": "DEBIT"},
                {"account_id": str(account2.id), "amount": "150.00", "direction": "CREDIT"}
            ]
        }
        
        code, body, _ = post_entry(tenant, payload, "idem_SPK1", get_hash(payload))
        assert code == 201
        
        assert JournalEntry.objects.count() == 1
        assert TransactionLine.objects.count() == 2
        
        res1 = BalanceQueryService.get_account_balance(account)
        res2 = BalanceQueryService.get_account_balance(account2)
        assert res1['balance'] == Decimal('150.0000')
        assert res2['balance'] == Decimal('150.0000')

@pytest.mark.django_db(transaction=True)
def test_post_entry_fails_without_tenant_context(managed_tenant_and_account):
    tenant, account = managed_tenant_and_account
    
    with connection.cursor() as c:
        c.execute('RESET ROLE;')
    account2 = Account.objects.create(tenant=tenant, name='Acc 4', type='LIABILITY', currency='USD')
    with connection.cursor() as c:
        c.execute('SET ROLE ledger_runtime;')

    payload = {
        "reference_id": "TX_SPK2_NO_CTX",
        "lines": [
            {"account_id": str(account.id), "amount": "50.00", "direction": "DEBIT"},
            {"account_id": str(account2.id), "amount": "50.00", "direction": "CREDIT"}
        ]
    }
    
    # Deliberately NOT setting app.tenant_id
    with pytest.raises(ProgrammingError, match="row-level security"):
        with transaction.atomic():
            post_entry(tenant, payload, "idem_SPK2_NO_CTX", get_hash(payload))

@pytest.mark.django_db(transaction=True)
def test_runtime_role_drops_bypassrls_and_superuser():
    with connection.cursor() as c:
        c.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user;")
        row = c.fetchone()
        assert row[0] is False
        assert row[1] is False

@pytest.mark.django_db
def test_rls_under_plain_django_db_wrapper(managed_tenant_and_account):
    tenant, account = managed_tenant_and_account
    
    with connection.cursor() as c:
        c.execute("SELECT current_user;")
        assert c.fetchone()[0] == 'ledger_runtime'

    assert Account.objects.count() == 0

    with transaction.atomic():
        with connection.cursor() as c:
            c.execute(f"SELECT set_config('app.tenant_id', '{tenant.id}', true);")
        assert Account.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.runtime_role
def test_runtime_cannot_mutate_ledger():
    with connection.cursor() as c:
        for t in ('journal_entries', 'transaction_lines'):
            for p in ('UPDATE', 'DELETE', 'TRUNCATE'):
                c.execute("SELECT has_table_privilege('ledger_runtime', %s, %s);", [t, p])
                assert c.fetchone()[0] is False, f"ledger_runtime should NOT have {p} on {t}"
