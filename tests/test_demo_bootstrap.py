import io
import re
import uuid
import pytest
from django.core.management import call_command
from django.db import connection
from ledger.models import Tenant, Account, Hold

@pytest.mark.django_db
def test_demo_bootstrap_command():
    out = io.StringIO()
    call_command('demo_bootstrap', name='Test Bootstrap Tenant', hours=12, stdout=out)
    output = out.getvalue()

    assert "TENANT_ID:" in output
    assert "TOKEN:" in output
    assert "Open http://localhost:8000/demo/ and paste the token (valid 12 hours)." in output

    tenant_id_match = re.search(r'TENANT_ID:\s*([a-f0-9\-]+)', output)
    assert tenant_id_match is not None
    tenant_id = uuid.UUID(tenant_id_match.group(1).strip())

    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])

    tenant = Tenant.objects.get(id=tenant_id)
    assert tenant.name == 'Test Bootstrap Tenant'

    accounts = Account.objects.filter(tenant=tenant)
    assert accounts.count() == 4
    account_names = set(accounts.values_list('name', flat=True))
    assert account_names == {"Cash", "Customer Deposits", "Revenue", "Expenses"}

    holds = Hold.objects.filter(tenant=tenant)
    assert holds.count() == 1
    assert holds.first().reference_id == 'demo-seed-hold'
