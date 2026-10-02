import pytest
from django.db import connection

@pytest.mark.django_db
def test_app_role_cannot_select_webhook_endpoints_without_context():
    with connection.cursor() as cursor:
        cursor.execute("DROP ROLE IF EXISTS restricted_app_role;")
        cursor.execute("CREATE ROLE restricted_app_role LOGIN;")
        cursor.execute("GRANT SELECT ON webhook_endpoints TO restricted_app_role;")
        
        cursor.execute("INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-000000000000', 'Test') ON CONFLICT DO NOTHING;")
        cursor.execute("INSERT INTO webhook_endpoints (id, tenant_id, url, secret, event_types) VALUES ('11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-000000000000', 'http://test.com', 'sec', '{entry.posted}') ON CONFLICT DO NOTHING;")
        
        cursor.execute("SET ROLE restricted_app_role;")
        cursor.execute("SELECT count(*) FROM webhook_endpoints;")
        count = cursor.fetchone()[0]
        
        assert count == 0
        cursor.execute("RESET ROLE;")
