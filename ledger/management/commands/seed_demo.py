import uuid
import json
import hashlib
from decimal import Decimal
import datetime
from django.utils import timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from ledger.models import Tenant, Account, Hold
from ledger.services.posting import post_entry
from ledger.services.holds import authorize_hold


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).digest()


class Command(BaseCommand):
    help = 'Seeds demo chart of accounts, sample journal entries, and a hold idempotently.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant_id', type=uuid.UUID, required=True, help='UUID of the tenant to seed')

    def handle(self, *args, **options):
        tenant_id = options['tenant_id']

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])

            try:
                tenant = Tenant.objects.get(id=tenant_id)
            except Tenant.DoesNotExist:
                raise CommandError(f"Tenant {tenant_id} does not exist.")

            # 1. Accounts
            cash, _ = Account.objects.get_or_create(
                tenant_id=tenant.id, name="Cash",
                defaults={'type': Account.Type.ASSET, 'currency': 'USD', 'allow_negative': False}
            )
            deposits, _ = Account.objects.get_or_create(
                tenant_id=tenant.id, name="Customer Deposits",
                defaults={'type': Account.Type.LIABILITY, 'currency': 'USD', 'allow_negative': False}
            )
            revenue, _ = Account.objects.get_or_create(
                tenant_id=tenant.id, name="Revenue",
                defaults={'type': Account.Type.REVENUE, 'currency': 'USD', 'allow_negative': False}
            )
            expenses, _ = Account.objects.get_or_create(
                tenant_id=tenant.id, name="Expenses",
                defaults={'type': Account.Type.EXPENSE, 'currency': 'USD', 'allow_negative': False}
            )

            # 2. Entries (Idempotent via post_entry with deterministic reference IDs & keys)
            # Entry 1: Seed $1,000.00 initial customer deposit
            p1 = {
                'reference_id': 'demo-seed-deposit',
                'description': 'Initial customer deposit into cash',
                'lines': [
                    {'account_id': str(cash.id), 'amount': '1000.0000', 'direction': 'DEBIT'},
                    {'account_id': str(deposits.id), 'amount': '1000.0000', 'direction': 'CREDIT'},
                ]
            }
            post_entry(tenant, p1, f'idem-demo-deposit-{tenant.id}', _hash(p1))

            # Entry 2: Customer payment of $150.00 to platform revenue
            p2 = {
                'reference_id': 'demo-seed-revenue',
                'description': 'Platform subscription fee payment',
                'lines': [
                    {'account_id': str(deposits.id), 'amount': '150.0000', 'direction': 'DEBIT'},
                    {'account_id': str(revenue.id), 'amount': '150.0000', 'direction': 'CREDIT'},
                ]
            }
            post_entry(tenant, p2, f'idem-demo-revenue-{tenant.id}', _hash(p2))

            # Entry 3: Office expense payment of $50.00 from cash
            p3 = {
                'reference_id': 'demo-seed-expense',
                'description': 'Server infrastructure expense',
                'lines': [
                    {'account_id': str(expenses.id), 'amount': '50.0000', 'direction': 'DEBIT'},
                    {'account_id': str(cash.id), 'amount': '50.0000', 'direction': 'CREDIT'},
                ]
            }
            post_entry(tenant, p3, f'idem-demo-expense-{tenant.id}', _hash(p3))

            # 3. Hold: Authorize a $100.00 hold on Customer Deposits
            existing_hold = Hold.objects.filter(tenant=tenant, reference_id='demo-seed-hold').first()
            if existing_hold:
                hold_id = existing_hold.id
            else:
                hp = {
                    'account_id': str(deposits.id),
                    'amount': '100.0000',
                    'currency': 'USD',
                    'reference_id': 'demo-seed-hold',
                    'description': 'Authorization for pending marketplace order',
                    'expires_at': (timezone.now() + datetime.timedelta(days=7)).isoformat()
                }
                c, b, _ = authorize_hold(tenant, hp, f'idem-demo-hold-{tenant.id}', _hash(hp))
                hold_id = b['hold_id']

        self.stdout.write(self.style.SUCCESS(f"Demo seeded successfully for tenant {tenant.id}!"))
        self.stdout.write(f"ACCOUNT_CASH: {cash.id}")
        self.stdout.write(f"ACCOUNT_CUSTOMER_DEPOSITS: {deposits.id}")
        self.stdout.write(f"ACCOUNT_REVENUE: {revenue.id}")
        self.stdout.write(f"ACCOUNT_EXPENSES: {expenses.id}")
        self.stdout.write(f"HOLD_ID: {hold_id}")
