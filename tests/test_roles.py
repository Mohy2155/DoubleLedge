import pytest
from django.db import connection

pytestmark = pytest.mark.runtime_role


@pytest.mark.django_db
def test_app_role_cannot_select_tenants_without_context():
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE;")
    try:
        with connection.cursor() as cursor:
            # Clean up and create restricted role
            cursor.execute("""
                DO $$
                BEGIN
                    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'restricted_app_role') THEN
                        DROP OWNED BY restricted_app_role;
                        DROP ROLE restricted_app_role;
                    END IF;
                END
                $$;
            """)
            cursor.execute("CREATE ROLE restricted_app_role LOGIN;")
            cursor.execute("GRANT SELECT ON tenants TO restricted_app_role;")
            
            # We need a tenant
            cursor.execute("INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-000000000000', 'Test') ON CONFLICT DO NOTHING;")
            
            # Switch to the role
            cursor.execute("SET ROLE restricted_app_role;")
            
            # Try to select
            cursor.execute("SELECT count(*) FROM tenants;")
            count = cursor.fetchone()[0]
            
            # Should be 0 due to RLS
            assert count == 0
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE;")
            cursor.execute("""
                DO $$
                BEGIN
                    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'restricted_app_role') THEN
                        DROP OWNED BY restricted_app_role;
                        DROP ROLE restricted_app_role;
                    END IF;
                END
                $$;
            """)
            cursor.execute("SET ROLE ledger_runtime;")

