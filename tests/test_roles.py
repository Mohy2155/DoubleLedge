import pytest
from django.db import connection

@pytest.mark.django_db
def test_app_role_cannot_select_tenants_without_context():
    with connection.cursor() as cursor:
        # Create a restricted role
        cursor.execute("DROP ROLE IF EXISTS restricted_app_role;")
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
        
        # Revert role
        cursor.execute("RESET ROLE;")
