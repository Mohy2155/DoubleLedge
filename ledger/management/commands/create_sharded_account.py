import uuid
from django.core.management.base import BaseCommand, CommandError
from ledger.models import Tenant
from ledger.services.sharding import create_sharded_account

class Command(BaseCommand):
    help = 'Creates a new account, optionally sharded.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant_id', type=uuid.UUID, required=True)
        parser.add_argument('--name', type=str, required=True)
        parser.add_argument('--type', type=str, required=True, choices=['ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE'])
        parser.add_argument('--currency', type=str, required=True)
        parser.add_argument('--allow_negative', action='store_true', default=False)
        parser.add_argument('--shard_count', type=int, default=0)

    def handle(self, *args, **options):
        tenant_id = options['tenant_id']
        name = options['name']
        account_type = options['type']
        currency = options['currency']
        allow_negative = options['allow_negative']
        shard_count = options['shard_count']

        if shard_count < 0 or shard_count > 64:
            raise CommandError("shard_count must be between 0 and 64")

        try:
            Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            raise CommandError(f"Tenant {tenant_id} does not exist.")

        account = create_sharded_account(
            tenant_id=tenant_id,
            name=name,
            account_type=account_type,
            currency=currency,
            allow_negative=allow_negative,
            shard_count=shard_count
        )

        self.stdout.write(self.style.SUCCESS(f"Successfully created account {account.id} with {shard_count} shards."))
