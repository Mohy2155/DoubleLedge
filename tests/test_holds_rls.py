import pytest
import uuid
from django.db import connection, ProgrammingError

@pytest.mark.django_db
def test_app_role_cannot_select_holds_without_context():
    with connection.cursor() as cursor:
        cursor.execute("DROP ROLE IF EXISTS restricted_app_role;")
        cursor.execute("CREATE ROLE restricted_app_role LOGIN;")
        cursor.execute("GRANT SELECT ON holds TO restricted_app_role;")
        
        tenant_id = '00000000-0000-0000-0000-000000000000'
        account_id = '11111111-1111-1111-1111-111111111111'
        hold_id = '22222222-2222-2222-2222-222222222222'
        
        cursor.execute("INSERT INTO tenants (id, name) VALUES (%s, 'Test') ON CONFLICT DO NOTHING;", [tenant_id])
        cursor.execute("INSERT INTO accounts (id, tenant_id, name, type, currency) VALUES (%s, %s, 'A', 'ASSET', 'USD') ON CONFLICT DO NOTHING;", [account_id, tenant_id])
        
        cursor.execute("""
            INSERT INTO holds (id, tenant_id, account_id, reference_id, currency, amount, expires_at) 
            VALUES (%s, %s, %s, 'ref-1', 'USD', 100, now() + interval '1 day') 
            ON CONFLICT DO NOTHING;
        """, [hold_id, tenant_id, account_id])
        
        cursor.execute("SET ROLE restricted_app_role;")
        cursor.execute("SELECT count(*) FROM holds;")
        count = cursor.fetchone()[0]
        
        assert count == 0
        cursor.execute("RESET ROLE;")

@pytest.mark.django_db
def test_cross_tenant_hold_visibility():
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())
    account_a = str(uuid.uuid4())
    hold_a = str(uuid.uuid4())

    with connection.cursor() as cursor:
        cursor.execute("DROP ROLE IF EXISTS restricted_app_role;")
        cursor.execute("CREATE ROLE restricted_app_role LOGIN;")
        cursor.execute("GRANT SELECT, INSERT ON holds TO restricted_app_role;")
        
        cursor.execute("INSERT INTO tenants (id, name) VALUES (%s, 'Tenant A'), (%s, 'Tenant B')", [tenant_a, tenant_b])
        cursor.execute("INSERT INTO accounts (id, tenant_id, name, type, currency) VALUES (%s, %s, 'A', 'ASSET', 'USD')", [account_a, tenant_a])
        
        # Insert a hold for Tenant A
        cursor.execute("SET ROLE restricted_app_role;")
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_a])
        cursor.execute("""
            INSERT INTO holds (id, tenant_id, account_id, reference_id, currency, amount, expires_at) 
            VALUES (%s, %s, %s, 'ref-a', 'USD', 100, now() + interval '1 day')
        """, [hold_a, tenant_a, account_a])
        
        # Tenant B cannot see Tenant A's hold
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_b])
        cursor.execute("SELECT count(*) FROM holds WHERE id = %s", [hold_a])
        count = cursor.fetchone()[0]
        assert count == 0
        
        # Tenant B cannot insert a hold with Tenant A's tenant_id (RLS WITH CHECK violation)
        from django.db import transaction
        with pytest.raises(ProgrammingError, match="new row violates row-level security policy"):
            with transaction.atomic():
                cursor.execute("""
                    INSERT INTO holds (id, tenant_id, account_id, reference_id, currency, amount, expires_at) 
                    VALUES (%s, %s, %s, 'ref-b', 'USD', 100, now() + interval '1 day')
                """, [str(uuid.uuid4()), tenant_a, account_a])
        
        cursor.execute("RESET ROLE;")
