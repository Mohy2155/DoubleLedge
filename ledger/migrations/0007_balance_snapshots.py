# Generated manually

from django.db import migrations, models
import django.utils.timezone
import uuid

class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0006_webhookdelivery_webhookendpoint'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            -- 1. Add sequence to journal_entries
            ALTER TABLE journal_entries ADD COLUMN sequence BIGINT GENERATED ALWAYS AS IDENTITY;
            
            -- 2. Create balance_snapshots table
            CREATE TABLE balance_snapshots (
                id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id          uuid NOT NULL REFERENCES tenants(id),
                account_id         uuid NOT NULL REFERENCES accounts(id),
                sequence           bigint NOT NULL,
                entry_id           uuid NOT NULL REFERENCES journal_entries(id),
                balance            numeric(28, 4) NOT NULL,
                debit_total        numeric(28, 4) NOT NULL,
                credit_total       numeric(28, 4) NOT NULL,
                line_count         bigint NOT NULL,
                max_posted_at      timestamptz NOT NULL,
                created_at         timestamptz NOT NULL DEFAULT now(),
                UNIQUE (tenant_id, account_id, sequence)
            );

            -- Composite index
            CREATE INDEX idx_balance_snapshots_account_seq ON balance_snapshots (tenant_id, account_id, sequence DESC);

            -- RLS
            ALTER TABLE balance_snapshots ENABLE ROW LEVEL SECURITY;
            ALTER TABLE balance_snapshots FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation_balance_snapshots ON balance_snapshots
                USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
            """,
            reverse_sql="""
            DROP TABLE IF EXISTS balance_snapshots CASCADE;
            ALTER TABLE journal_entries DROP COLUMN IF EXISTS sequence;
            """
        ),
        migrations.CreateModel(
            name='BalanceSnapshot',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('sequence', models.BigIntegerField()),
                ('balance', models.DecimalField(decimal_places=4, max_digits=28)),
                ('debit_total', models.DecimalField(decimal_places=4, max_digits=28)),
                ('credit_total', models.DecimalField(decimal_places=4, max_digits=28)),
                ('line_count', models.BigIntegerField()),
                ('max_posted_at', models.DateTimeField()),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                'db_table': 'balance_snapshots',
                'managed': False,
            },
        ),
    ]
