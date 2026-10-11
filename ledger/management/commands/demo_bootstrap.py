import io
import uuid
import re
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from ledger.models import Tenant, Account, Hold

class Command(BaseCommand):
    help = 'Bootstrap tenant and demo data in one command.'

    def add_arguments(self, parser):
        parser.add_argument('--name', type=str, default='Demo Tenant', help='Name of the tenant')
        parser.add_argument('--hours', type=int, default=24, help='Token lifetime in hours (default: 24)')

    def handle(self, *args, **options):
        name = options['name']
        hours = options['hours']

        out = io.StringIO()
        call_command('create_tenant', name=name, hours=hours, stdout=out)
        output_text = out.getvalue()

        tenant_id_match = re.search(r'TENANT_ID:\s*([a-f0-9\-]+)', output_text)
        token_match = re.search(r'TOKEN:\s*([^\s]+)', output_text)

        if not tenant_id_match or not token_match:
            raise CommandError("Failed to parse tenant creation output.")

        tenant_id_str = tenant_id_match.group(1).strip()
        token_str = token_match.group(1).strip()
        tenant_uuid = uuid.UUID(tenant_id_str)

        seed_out = io.StringIO()
        call_command('seed_demo', tenant_id=tenant_uuid, stdout=seed_out)

        self.stdout.write(f"TENANT_ID: {tenant_id_str}")
        self.stdout.write(f"TOKEN: {token_str}")
        self.stdout.write(f"Open http://localhost:8000/demo/ and paste the token (valid {hours} hours).")
