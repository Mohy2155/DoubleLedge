from django.db import migrations, models
import django.utils.timezone
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0012_runtime_grants'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            -- NULL = no scheduled close. Capped at 27 so the previous month is always eligible.
            ALTER TABLE tenants ADD COLUMN period_close_grace_days integer
                CHECK (period_close_grace_days BETWEEN 0 AND 27);

            -- Append-only history; a tenant's cutoff is max(cutoff). effective_at < cutoff is frozen.
            CREATE TABLE period_closes (
                id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id  uuid        NOT NULL REFERENCES tenants(id),
                cutoff     timestamptz NOT NULL,
                source     text        NOT NULL CHECK (source IN ('MANUAL', 'SCHEDULED')),
                created_at timestamptz NOT NULL DEFAULT now()
            );

            CREATE INDEX idx_period_closes_tenant_cutoff ON period_closes (tenant_id, cutoff DESC);

            ALTER TABLE period_closes ENABLE ROW LEVEL SECURITY;
            ALTER TABLE period_closes FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation_period_closes ON period_closes
                USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

            CREATE TRIGGER trg_period_closes_immutable
                BEFORE UPDATE OR DELETE ON period_closes
                FOR EACH ROW EXECUTE FUNCTION block_modification();

            CREATE TRIGGER trg_period_closes_immutable_trunc
                BEFORE TRUNCATE ON period_closes
                FOR EACH STATEMENT EXECUTE FUNCTION block_modification();

            -- Postings take the per-tenant advisory lock (7301, hashtext(tenant)) SHARED, closes take it
            -- EXCLUSIVE, so a close waits for in-flight postings and no posting reads a stale cutoff.
            -- The two-int4 key space does not overlap the bigint locks used by Celery tasks.
            CREATE FUNCTION assert_period_open() RETURNS trigger AS $$
            DECLARE
                v_effective_at timestamptz;
                v_cutoff       timestamptz;
            BEGIN
                IF TG_TABLE_NAME = 'journal_entries' THEN
                    v_effective_at := NEW.effective_at;
                ELSE
                    SELECT effective_at INTO v_effective_at
                      FROM journal_entries WHERE id = NEW.journal_entry_id;
                END IF;

                PERFORM pg_advisory_xact_lock_shared(7301, hashtext(NEW.tenant_id::text));

                SELECT max(cutoff) INTO v_cutoff FROM period_closes WHERE tenant_id = NEW.tenant_id;
                IF v_cutoff IS NOT NULL AND v_effective_at < v_cutoff THEN
                    RAISE EXCEPTION 'period_closed: effective_at % is before closed cutoff %',
                        v_effective_at, v_cutoff
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER trg_journal_entries_period_open
                BEFORE INSERT ON journal_entries
                FOR EACH ROW EXECUTE FUNCTION assert_period_open();

            CREATE TRIGGER trg_transaction_lines_period_open
                BEFORE INSERT ON transaction_lines
                FOR EACH ROW EXECUTE FUNCTION assert_period_open();

            CREATE FUNCTION assert_period_close_valid() RETURNS trigger AS $$
            DECLARE
                v_cutoff timestamptz;
            BEGIN
                PERFORM pg_advisory_xact_lock(7301, hashtext(NEW.tenant_id::text));

                IF NEW.cutoff > now() THEN
                    RAISE EXCEPTION 'invalid_period_cutoff: cutoff % is in the future', NEW.cutoff
                        USING ERRCODE = '23514';
                END IF;

                SELECT max(cutoff) INTO v_cutoff FROM period_closes WHERE tenant_id = NEW.tenant_id;
                IF v_cutoff IS NOT NULL AND NEW.cutoff <= v_cutoff THEN
                    RAISE EXCEPTION 'invalid_period_cutoff: cutoff % does not advance current cutoff %',
                        NEW.cutoff, v_cutoff
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER trg_period_closes_valid
                BEFORE INSERT ON period_closes
                FOR EACH ROW EXECUTE FUNCTION assert_period_close_valid();

            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_runtime') THEN
                    GRANT SELECT, INSERT ON period_closes TO ledger_runtime;
                END IF;
            END
            $$;
            """,
            reverse_sql="""
            DROP TRIGGER IF EXISTS trg_journal_entries_period_open ON journal_entries;
            DROP TRIGGER IF EXISTS trg_transaction_lines_period_open ON transaction_lines;
            DROP TABLE IF EXISTS period_closes CASCADE;
            DROP FUNCTION IF EXISTS assert_period_open();
            DROP FUNCTION IF EXISTS assert_period_close_valid();
            ALTER TABLE tenants DROP COLUMN IF EXISTS period_close_grace_days;
            """
        ),
        migrations.CreateModel(
            name='PeriodClose',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('cutoff', models.DateTimeField()),
                ('source', models.TextField(choices=[('MANUAL', 'Manual'), ('SCHEDULED', 'Scheduled')])),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                'db_table': 'period_closes',
                'managed': False,
            },
        ),
    ]
