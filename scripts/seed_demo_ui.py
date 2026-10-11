import os
import sys
import uuid
import datetime
import jwt

def main():
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()

    from django.db import connection, transaction
    from ledger.models import Tenant
    from ledger.management.commands.create_tenant import get_or_create_keypair
    from django.core.management import call_command

    priv_pem, _ = get_or_create_keypair()
    tenant_id = uuid.uuid4()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])
        tenant = Tenant.objects.create(id=tenant_id, name="Acme Corp Demo")

    payload = {
        'tenant_id': str(tenant.id),
        'exp': datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)
    }
    token = jwt.encode(payload, priv_pem, algorithm='RS256')

    call_command('seed_demo', tenant_id=tenant.id)

    print("\n" + "=" * 70)
    print("DoubleLedge Demo UI is ready!")
    print("URL:   http://localhost:8000/demo/")
    print(f"Token: {token}")
    print("=" * 70 + "\n")

if __name__ == '__main__':
    main()
