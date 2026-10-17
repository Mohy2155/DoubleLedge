import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from ledger.models import Tenant


class Command(BaseCommand):
    help = 'Sets or disables the scheduled monthly period close for a tenant. Runs as the owner role.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant_id', type=uuid.UUID, required=True)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--grace_days', type=int, help='Days after a month ends before it is closed (0-27).')
        group.add_argument('--disable', action='store_true')

    def handle(self, *args, **options):
        tenant_id = options['tenant_id']
        grace_days = None if options['disable'] else options['grace_days']
        if grace_days is not None and not 0 <= grace_days <= 27:
            raise CommandError("grace_days must be between 0 and 27")

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])
            updated = Tenant.objects.filter(id=tenant_id).update(period_close_grace_days=grace_days)

        if not updated:
            raise CommandError(f"Tenant {tenant_id} does not exist.")

        state = "disabled" if grace_days is None else f"{grace_days} grace days"
        self.stdout.write(self.style.SUCCESS(f"Scheduled period close for tenant {tenant_id}: {state}."))
