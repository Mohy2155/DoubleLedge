from django.db import migrations, models
import django.utils.timezone
import uuid

class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0007_balance_snapshots'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            CREATE TABLE holds (
                id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id         uuid        NOT NULL REFERENCES tenants(id),
                reference_id      text        NOT NULL,
                account_id        uuid        NOT NULL,
                currency          char(3)     NOT NULL,
                amount            numeric(19, 4) NOT NULL CHECK (amount > 0),
                captured_amount   numeric(19, 4) NOT NULL DEFAULT 0 CHECK (captured_amount >= 0),
                status            text        NOT NULL DEFAULT 'PENDING'
                                  CHECK (status IN ('PENDING', 'CAPTURED', 'VOIDED', 'EXPIRED')),
                description       text        NOT NULL DEFAULT '',
                journal_entry_id  uuid,
                expires_at        timestamptz NOT NULL,
                created_at        timestamptz NOT NULL DEFAULT now(),
                updated_at        timestamptz NOT NULL DEFAULT now(),
                metadata          jsonb       NOT NULL DEFAULT '{}',
                UNIQUE (tenant_id, reference_id),
                UNIQUE (tenant_id, id),
                FOREIGN KEY (tenant_id, account_id)       REFERENCES accounts (tenant_id, id),
                FOREIGN KEY (tenant_id, journal_entry_id) REFERENCES journal_entries (tenant_id, id),
                CHECK (captured_amount <= amount)
            );

            CREATE INDEX idx_holds_pending
                ON holds (tenant_id, account_id)
                WHERE status = 'PENDING';

            CREATE INDEX idx_holds_pending_expiry
                ON holds (expires_at)
                WHERE status = 'PENDING';

            CREATE FUNCTION update_holds_timestamp() RETURNS trigger AS $$
            BEGIN
                NEW.updated_at = now();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER trg_holds_updated_at
                BEFORE UPDATE ON holds
                FOR EACH ROW EXECUTE FUNCTION update_holds_timestamp();

            CREATE FUNCTION block_terminal_hold_update() RETURNS trigger AS $$
            BEGIN
                IF OLD.status IN ('CAPTURED', 'VOIDED', 'EXPIRED') THEN
                    RAISE EXCEPTION 'Cannot modify hold % in terminal state %', OLD.id, OLD.status;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER trg_holds_terminal_immutable
                BEFORE UPDATE ON holds
                FOR EACH ROW EXECUTE FUNCTION block_terminal_hold_update();

            ALTER TABLE holds ENABLE ROW LEVEL SECURITY;
            ALTER TABLE holds FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation_holds ON holds
                USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
            """,
            reverse_sql="""
            DROP TABLE IF EXISTS holds CASCADE;
            DROP FUNCTION IF EXISTS update_holds_timestamp() CASCADE;
            DROP FUNCTION IF EXISTS block_terminal_hold_update() CASCADE;
            """
        ),
        migrations.CreateModel(
            name='Hold',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('reference_id', models.TextField()),
                ('currency', models.CharField(max_length=3)),
                ('amount', models.DecimalField(decimal_places=4, max_digits=19)),
                ('captured_amount', models.DecimalField(decimal_places=4, default=0, max_digits=19)),
                ('status', models.TextField(choices=[('PENDING', 'Pending'), ('CAPTURED', 'Captured'), ('VOIDED', 'Voided'), ('EXPIRED', 'Expired')], default='PENDING')),
                ('description', models.TextField(default='')),
                ('expires_at', models.DateTimeField()),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('metadata', models.JSONField(default=dict)),
            ],
            options={
                'db_table': 'holds',
                'managed': False,
            },
        ),
    ]
