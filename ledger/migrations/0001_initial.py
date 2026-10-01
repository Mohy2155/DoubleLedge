from django.db import migrations


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.RunSQL(
            sql="""
            CREATE TABLE tenants (id uuid PRIMARY KEY, name text NOT NULL);

            CREATE TABLE accounts (
                id            uuid PRIMARY KEY,
                tenant_id     uuid    NOT NULL REFERENCES tenants(id),
                name          text    NOT NULL,
                type          text    NOT NULL CHECK (type IN ('ASSET','LIABILITY','EQUITY','REVENUE','EXPENSE')),
                currency      char(3) NOT NULL,
                balance       numeric(19,4) NOT NULL DEFAULT 0,
                allow_negative boolean NOT NULL DEFAULT false,
                version       bigint  NOT NULL DEFAULT 0,
                UNIQUE (tenant_id, id),
                CHECK (allow_negative OR balance >= 0)
            );

            CREATE TABLE journal_entries (
                id            uuid PRIMARY KEY,
                tenant_id     uuid NOT NULL REFERENCES tenants(id),
                reference_id  text NOT NULL,
                description   text NOT NULL,
                reverses_id   uuid REFERENCES journal_entries(id),
                effective_at  timestamptz NOT NULL DEFAULT now(),
                posted_at     timestamptz NOT NULL DEFAULT now(),
                metadata      jsonb NOT NULL DEFAULT '{}',
                UNIQUE (tenant_id, reference_id),
                UNIQUE (tenant_id, id),
                UNIQUE (reverses_id)
            );

            CREATE TABLE transaction_lines (
                id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                tenant_id        uuid NOT NULL,
                journal_entry_id uuid NOT NULL,
                account_id       uuid NOT NULL,
                direction        text NOT NULL CHECK (direction IN ('DEBIT','CREDIT')),
                amount           numeric(19,4) NOT NULL CHECK (amount > 0),
                FOREIGN KEY (tenant_id, journal_entry_id) REFERENCES journal_entries (tenant_id, id),
                FOREIGN KEY (tenant_id, account_id)       REFERENCES accounts (tenant_id, id)
            );

            CREATE TABLE outbox_events (
                id uuid PRIMARY KEY, tenant_id uuid NOT NULL, type text NOT NULL,
                payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
                delivered_at timestamptz, attempts int NOT NULL DEFAULT 0
            );

            CREATE TABLE ledger_checkpoints (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                tenant_id uuid NOT NULL, up_to_line_id bigint NOT NULL,
                batch_hash bytea NOT NULL, prev_hash bytea, created_at timestamptz NOT NULL DEFAULT now()
            );

            CREATE TABLE idempotency_keys (
                tenant_id     uuid        NOT NULL,
                key           text        NOT NULL,
                request_hash  bytea       NOT NULL,
                response_code int,
                response_body jsonb,
                created_at    timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (tenant_id, key)
            );

                        CREATE FUNCTION assert_entry_balanced_from_entry() RETURNS trigger AS $$
            DECLARE
                v_lines int;
                v_bad   record;
            BEGIN
                SELECT count(*) INTO v_lines
                  FROM transaction_lines WHERE journal_entry_id = NEW.id;
                IF v_lines < 2 THEN
                    RAISE EXCEPTION 'journal entry % needs at least 2 lines', NEW.id
                        USING ERRCODE = '23514';
                END IF;

                SELECT a.currency, SUM(CASE l.direction WHEN 'DEBIT' THEN l.amount ELSE -l.amount END) as net
                  INTO v_bad
                  FROM transaction_lines l
                  JOIN accounts a ON a.id = l.account_id
                 WHERE l.journal_entry_id = NEW.id
                 GROUP BY a.currency
                HAVING SUM(CASE l.direction WHEN 'DEBIT' THEN l.amount ELSE -l.amount END) <> 0
                 LIMIT 1;

                IF FOUND THEN
                    RAISE EXCEPTION 'journal entry % unbalanced in % (net %)',
                        NEW.id, v_bad.currency, v_bad.net
                        USING ERRCODE = '23514';
                END IF;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;

            

            CREATE CONSTRAINT TRIGGER trg_entry_balanced_entry
                AFTER INSERT ON journal_entries
                DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced_from_entry();

            -- Immutability triggers
            CREATE FUNCTION block_modification() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'Modification of ledger history is forbidden';
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER trg_journal_entries_immutable
                BEFORE UPDATE OR DELETE ON journal_entries
                FOR EACH ROW EXECUTE FUNCTION block_modification();

            CREATE TRIGGER trg_transaction_lines_immutable
                BEFORE UPDATE OR DELETE ON transaction_lines
                FOR EACH ROW EXECUTE FUNCTION block_modification();

            CREATE TRIGGER trg_journal_entries_immutable_trunc
                BEFORE TRUNCATE ON journal_entries
                FOR EACH STATEMENT EXECUTE FUNCTION block_modification();

            CREATE TRIGGER trg_transaction_lines_immutable_trunc
                BEFORE TRUNCATE ON transaction_lines
                FOR EACH STATEMENT EXECUTE FUNCTION block_modification();

            -- Row Level Security
            ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
            ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation_tenants ON tenants
                USING      (id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (id = nullif(current_setting('app.tenant_id', true), '')::uuid);

            ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
            ALTER TABLE accounts FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation_accounts ON accounts
                USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

            ALTER TABLE journal_entries ENABLE ROW LEVEL SECURITY;
            ALTER TABLE journal_entries FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation_entries ON journal_entries
                USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

            ALTER TABLE transaction_lines ENABLE ROW LEVEL SECURITY;
            ALTER TABLE transaction_lines FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation_lines ON transaction_lines
                USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

            ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY;
            ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation_outbox ON outbox_events
                USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

            ALTER TABLE ledger_checkpoints ENABLE ROW LEVEL SECURITY;
            ALTER TABLE ledger_checkpoints FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation_checkpoints ON ledger_checkpoints
                USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

            ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY;
            ALTER TABLE idempotency_keys FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation_idempotency ON idempotency_keys
                USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
            """,
            reverse_sql="""
            DROP TABLE IF EXISTS idempotency_keys CASCADE;
            DROP TABLE IF EXISTS ledger_checkpoints CASCADE;
            DROP TABLE IF EXISTS outbox_events CASCADE;
            DROP TABLE IF EXISTS transaction_lines CASCADE;
            DROP TABLE IF EXISTS journal_entries CASCADE;
            DROP TABLE IF EXISTS accounts CASCADE;
            DROP TABLE IF EXISTS tenants CASCADE;
            DROP FUNCTION IF EXISTS assert_entry_balanced_from_entry() CASCADE;
            DROP FUNCTION IF EXISTS block_modification() CASCADE;
            """
        )
    ]
