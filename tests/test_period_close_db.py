import datetime
import uuid

import pytest
from django.db import DatabaseError, IntegrityError, ProgrammingError, connection, transaction
from django.utils import timezone

from ledger.models import Account, Tenant

CUTOFF = timezone.now().replace(microsecond=0) - datetime.timedelta(days=10)


@pytest.fixture
def tenant():
    return Tenant.objects.create(name="Period DB Tenant")


@pytest.fixture
def accounts(tenant):
    bank = Account.objects.create(tenant=tenant, name="Bank", type=Account.Type.ASSET, currency="USD", allow_negative=True)
    sales = Account.objects.create(tenant=tenant, name="Sales", type=Account.Type.REVENUE, currency="USD")
    return bank, sales


def insert_entry(cursor, tenant_id, effective_at):
    entry_id = str(uuid.uuid4())
    cursor.execute(
        "INSERT INTO journal_entries (id, tenant_id, reference_id, description, effective_at) VALUES (%s, %s, %s, '', %s)",
        [entry_id, str(tenant_id), f"ref-{entry_id}", effective_at],
    )
    return entry_id


def insert_line(cursor, tenant_id, entry_id, account_id, direction):
    cursor.execute(
        "INSERT INTO transaction_lines (tenant_id, journal_entry_id, account_id, direction, amount) VALUES (%s, %s, %s, %s, 10)",
        [str(tenant_id), entry_id, str(account_id), direction],
    )


def insert_balanced_entry(cursor, tenant_id, effective_at, debit_id, credit_id):
    entry_id = insert_entry(cursor, tenant_id, effective_at)
    insert_line(cursor, tenant_id, entry_id, debit_id, 'DEBIT')
    insert_line(cursor, tenant_id, entry_id, credit_id, 'CREDIT')
    return entry_id


def close(cursor, tenant_id, cutoff):
    cursor.execute(
        "INSERT INTO period_closes (tenant_id, cutoff, source) VALUES (%s, %s, 'MANUAL')",
        [str(tenant_id), cutoff],
    )


@pytest.mark.django_db
def test_entry_before_cutoff_is_rejected(tenant):
    with connection.cursor() as c:
        close(c, tenant.id, CUTOFF)
        with pytest.raises(IntegrityError, match="period_closed"):
            with transaction.atomic():
                insert_entry(c, tenant.id, CUTOFF - datetime.timedelta(seconds=1))


@pytest.mark.django_db
def test_entry_at_cutoff_is_accepted(tenant, accounts):
    bank, sales = accounts
    with connection.cursor() as c:
        close(c, tenant.id, CUTOFF)
        insert_balanced_entry(c, tenant.id, CUTOFF, bank.id, sales.id)


@pytest.mark.django_db
def test_line_for_entry_in_closed_period_is_rejected(tenant, accounts):
    bank, sales = accounts
    with connection.cursor() as c:
        entry_id = insert_balanced_entry(c, tenant.id, CUTOFF - datetime.timedelta(days=1), bank.id, sales.id)
        close(c, tenant.id, CUTOFF)
        with pytest.raises(IntegrityError, match="period_closed"):
            with transaction.atomic():
                insert_line(c, tenant.id, entry_id, bank.id, 'DEBIT')


@pytest.mark.django_db
@pytest.mark.parametrize("delta", [datetime.timedelta(0), -datetime.timedelta(days=1)])
def test_close_must_advance_cutoff(tenant, delta):
    with connection.cursor() as c:
        close(c, tenant.id, CUTOFF)
        with pytest.raises(IntegrityError, match="invalid_period_cutoff"):
            with transaction.atomic():
                close(c, tenant.id, CUTOFF + delta)


@pytest.mark.django_db
def test_close_in_future_is_rejected(tenant):
    with connection.cursor() as c:
        with pytest.raises(IntegrityError, match="invalid_period_cutoff"):
            with transaction.atomic():
                close(c, tenant.id, timezone.now() + datetime.timedelta(hours=1))


@pytest.mark.django_db
@pytest.mark.parametrize("statement", [
    "UPDATE period_closes SET cutoff = cutoff - interval '1 year'",
    "DELETE FROM period_closes",
    "TRUNCATE period_closes",
])
def test_period_closes_are_append_only(tenant, statement):
    with connection.cursor() as c:
        close(c, tenant.id, CUTOFF)
        with pytest.raises(DatabaseError, match="forbidden"):
            with transaction.atomic():
                c.execute(statement)


@pytest.mark.django_db
def test_cutoff_is_per_tenant(tenant):
    other = Tenant.objects.create(name="Other Tenant")
    bank = Account.objects.create(tenant=other, name="Bank", type=Account.Type.ASSET, currency="USD", allow_negative=True)
    sales = Account.objects.create(tenant=other, name="Sales", type=Account.Type.REVENUE, currency="USD")
    with connection.cursor() as c:
        close(c, tenant.id, CUTOFF)
        insert_balanced_entry(c, other.id, CUTOFF - datetime.timedelta(days=30), bank.id, sales.id)


@pytest.mark.django_db
@pytest.mark.runtime_role
def test_runtime_role_isolation_and_no_reopen():
    tenant_a = Tenant.objects.create(name="Tenant A")
    tenant_b = Tenant.objects.create(name="Tenant B")
    with connection.cursor() as c:
        c.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_a.id)])
        close(c, tenant_a.id, CUTOFF)

        with pytest.raises(ProgrammingError, match="permission denied"):
            with transaction.atomic():
                c.execute("UPDATE period_closes SET cutoff = cutoff - interval '1 year'")

        c.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_b.id)])
        c.execute("SELECT count(*) FROM period_closes")
        assert c.fetchone()[0] == 0

        with pytest.raises(ProgrammingError, match="row-level security"):
            with transaction.atomic():
                close(c, tenant_a.id, CUTOFF + datetime.timedelta(days=1))

        # Tenant A's cutoff does not leak into tenant B's postings.
        bank, sales = str(uuid.uuid4()), str(uuid.uuid4())
        c.execute(
            "INSERT INTO accounts (id, tenant_id, name, type, currency, allow_negative) VALUES "
            "(%s, %s, 'Bank', 'ASSET', 'USD', true), (%s, %s, 'Sales', 'REVENUE', 'USD', false)",
            [bank, str(tenant_b.id), sales, str(tenant_b.id)],
        )
        insert_balanced_entry(c, tenant_b.id, CUTOFF - datetime.timedelta(days=1), bank, sales)
