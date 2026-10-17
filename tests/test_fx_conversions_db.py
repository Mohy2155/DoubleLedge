import uuid

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction

from ledger.models import Account, Tenant


@pytest.fixture
def tenant():
    return Tenant.objects.create(name="FX DB Tenant")


def make_accounts(tenant):
    usd = Account.objects.create(tenant=tenant, name="Cash", type=Account.Type.ASSET, currency="USD", allow_negative=True)
    eur = Account.objects.create(tenant=tenant, name="Cash EUR", type=Account.Type.ASSET, currency="EUR", allow_negative=True)
    return usd, eur


def insert_entry(cursor, tenant_id, account_id):
    # Balanced, so the deferred entry triggers pass when the test checks constraints at teardown.
    entry_id = str(uuid.uuid4())
    cursor.execute(
        "INSERT INTO journal_entries (id, tenant_id, reference_id, description) VALUES (%s, %s, %s, '')",
        [entry_id, str(tenant_id), f"ref-{entry_id}"],
    )
    for direction in ("DEBIT", "CREDIT"):
        cursor.execute(
            "INSERT INTO transaction_lines (tenant_id, journal_entry_id, account_id, direction, amount) VALUES (%s, %s, %s, %s, 10)",
            [str(tenant_id), entry_id, str(account_id), direction],
        )
    return entry_id


def insert_conversion(cursor, tenant_id, entry_id, from_id, to_id, **overrides):
    row = {
        "from_currency": "USD", "to_currency": "EUR",
        "from_amount": "100.0000", "rate": "0.9234", "to_amount": "92.3400",
        **overrides,
    }
    cursor.execute(
        "INSERT INTO fx_conversions (tenant_id, journal_entry_id, from_account_id, to_account_id, from_currency, "
        "to_currency, from_amount, to_amount, rate, rate_as_of) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
        [str(tenant_id), entry_id, str(from_id), str(to_id), row["from_currency"], row["to_currency"],
         row["from_amount"], row["to_amount"], row["rate"]],
    )


@pytest.mark.django_db
@pytest.mark.parametrize("to_amount, accepted", [("0.1235", True), ("0.1234", False)])
def test_to_amount_must_follow_half_up_rounding(tenant, to_amount, accepted):
    usd, eur = make_accounts(tenant)
    with connection.cursor() as c:
        entry_id = insert_entry(c, tenant.id, usd.id)
        row = {"from_amount": "1.0000", "rate": "0.12345", "to_amount": to_amount}
        if accepted:
            insert_conversion(c, tenant.id, entry_id, usd.id, eur.id, **row)
        else:
            with pytest.raises(IntegrityError, match="check constraint"):
                with transaction.atomic():
                    insert_conversion(c, tenant.id, entry_id, usd.id, eur.id, **row)


@pytest.mark.django_db
def test_equal_currencies_are_rejected(tenant):
    usd, eur = make_accounts(tenant)
    with connection.cursor() as c:
        entry_id = insert_entry(c, tenant.id, usd.id)
        with pytest.raises(IntegrityError, match="check constraint"):
            with transaction.atomic():
                insert_conversion(c, tenant.id, entry_id, usd.id, eur.id, to_currency="USD")


@pytest.mark.django_db
@pytest.mark.parametrize("statement", [
    "UPDATE fx_conversions SET rate = rate * 2, to_amount = round(from_amount * rate * 2, 4)",
    "DELETE FROM fx_conversions",
    "TRUNCATE fx_conversions",
])
def test_fx_conversions_are_append_only(tenant, statement):
    usd, eur = make_accounts(tenant)
    with connection.cursor() as c:
        insert_conversion(c, tenant.id, insert_entry(c, tenant.id, usd.id), usd.id, eur.id)
        with pytest.raises(DatabaseError, match="forbidden"):
            with transaction.atomic():
                c.execute(statement)


@pytest.mark.django_db
def test_cross_tenant_references_are_rejected(tenant):
    usd, eur = make_accounts(tenant)
    other = Tenant.objects.create(name="Other FX Tenant")
    other_usd, other_eur = make_accounts(other)
    with connection.cursor() as c:
        entry_id = insert_entry(c, tenant.id, usd.id)
        other_entry_id = insert_entry(c, other.id, other_usd.id)
        with pytest.raises(IntegrityError, match="foreign key"):
            with transaction.atomic():
                insert_conversion(c, tenant.id, entry_id, usd.id, other_eur.id)
        with pytest.raises(IntegrityError, match="foreign key"):
            with transaction.atomic():
                insert_conversion(c, tenant.id, other_entry_id, usd.id, eur.id)


@pytest.mark.django_db
@pytest.mark.runtime_role
def test_runtime_role_sees_only_own_tenant():
    tenant_a = Tenant.objects.create(name="FX Tenant A")
    tenant_b = Tenant.objects.create(name="FX Tenant B")
    with connection.cursor() as c:
        c.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_a.id)])
        usd, eur = make_accounts(tenant_a)
        insert_conversion(c, tenant_a.id, insert_entry(c, tenant_a.id, usd.id), usd.id, eur.id)
        c.execute("SELECT count(*) FROM fx_conversions")
        assert c.fetchone()[0] == 1

        c.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_b.id)])
        c.execute("SELECT count(*) FROM fx_conversions")
        assert c.fetchone()[0] == 0
