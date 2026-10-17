from django.db import migrations, models
import django.utils.timezone
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0013_period_close'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            -- Append-only rate stamp for each conversion entry (ledger.services.fx).
            -- Composite FKs keep every reference inside the row's tenant.
            CREATE TABLE fx_conversions (
                id               uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id        uuid           NOT NULL REFERENCES tenants(id),
                journal_entry_id uuid           NOT NULL UNIQUE,
                from_account_id  uuid           NOT NULL,
                to_account_id    uuid           NOT NULL,
                from_currency    char(3)        NOT NULL,
                to_currency      char(3)        NOT NULL CHECK (from_currency <> to_currency),
                from_amount      numeric(19,4)  NOT NULL CHECK (from_amount > 0),
                to_amount        numeric(19,4)  NOT NULL CHECK (to_amount > 0),
                rate             numeric(28,12) NOT NULL CHECK (rate > 0),
                rate_source      text           NOT NULL DEFAULT '',
                rate_as_of       timestamptz    NOT NULL,
                created_at       timestamptz    NOT NULL DEFAULT now(),
                -- Half-up to 4 places: round(numeric) rounds half away from zero, the same for positive values.
                CHECK (to_amount = round(from_amount * rate, 4)),
                FOREIGN KEY (tenant_id, journal_entry_id) REFERENCES journal_entries (tenant_id, id),
                FOREIGN KEY (tenant_id, from_account_id)  REFERENCES accounts (tenant_id, id),
                FOREIGN KEY (tenant_id, to_account_id)    REFERENCES accounts (tenant_id, id)
            );

            CREATE INDEX idx_fx_conversions_tenant_created ON fx_conversions (tenant_id, created_at DESC);

            ALTER TABLE fx_conversions ENABLE ROW LEVEL SECURITY;
            ALTER TABLE fx_conversions FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation_fx_conversions ON fx_conversions
                USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

            CREATE TRIGGER trg_fx_conversions_immutable
                BEFORE UPDATE OR DELETE ON fx_conversions
                FOR EACH ROW EXECUTE FUNCTION block_modification();

            CREATE TRIGGER trg_fx_conversions_immutable_trunc
                BEFORE TRUNCATE ON fx_conversions
                FOR EACH STATEMENT EXECUTE FUNCTION block_modification();

            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_runtime') THEN
                    GRANT SELECT, INSERT ON fx_conversions TO ledger_runtime;
                END IF;
            END
            $$;
            """,
            reverse_sql="""
            DROP TABLE IF EXISTS fx_conversions CASCADE;
            """
        ),
        migrations.CreateModel(
            name='FxConversion',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('from_currency', models.CharField(max_length=3)),
                ('to_currency', models.CharField(max_length=3)),
                ('from_amount', models.DecimalField(decimal_places=4, max_digits=19)),
                ('to_amount', models.DecimalField(decimal_places=4, max_digits=19)),
                ('rate', models.DecimalField(decimal_places=12, max_digits=28)),
                ('rate_source', models.TextField(default='')),
                ('rate_as_of', models.DateTimeField()),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                'db_table': 'fx_conversions',
                'managed': False,
            },
        ),
    ]
