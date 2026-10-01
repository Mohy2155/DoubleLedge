import re

trigger_sql = """
            CREATE FUNCTION assert_entry_balanced_from_line() RETURNS trigger AS $$
            DECLARE
                v_lines int;
                v_bad   record;
            BEGIN
                SELECT count(*) INTO v_lines
                  FROM transaction_lines WHERE journal_entry_id = NEW.journal_entry_id;
                IF v_lines < 2 THEN
                    RAISE EXCEPTION 'journal entry % needs at least 2 lines', NEW.journal_entry_id
                        USING ERRCODE = '23514';
                END IF;

                SELECT a.currency, SUM(CASE l.direction WHEN 'DEBIT' THEN l.amount ELSE -l.amount END) as net
                  INTO v_bad
                  FROM transaction_lines l
                  JOIN accounts a ON a.id = l.account_id
                 WHERE l.journal_entry_id = NEW.journal_entry_id
                 GROUP BY a.currency
                HAVING SUM(CASE l.direction WHEN 'DEBIT' THEN l.amount ELSE -l.amount END) <> 0
                 LIMIT 1;

                IF FOUND THEN
                    RAISE EXCEPTION 'journal entry % unbalanced in % (net %)',
                        NEW.journal_entry_id, v_bad.currency, v_bad.net
                        USING ERRCODE = '23514';
                END IF;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;

            CREATE CONSTRAINT TRIGGER trg_entry_balanced_lines
                AFTER INSERT ON transaction_lines
                DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced_from_line();

            CREATE CONSTRAINT TRIGGER trg_entry_balanced_entry"""

with open('ledger/migrations/0001_initial.py', 'r') as f:
    content = f.read()

content = content.replace("CREATE CONSTRAINT TRIGGER trg_entry_balanced_entry", trigger_sql)
content = content.replace(
    "DROP FUNCTION IF EXISTS assert_entry_balanced_from_entry() CASCADE;",
    "DROP FUNCTION IF EXISTS assert_entry_balanced_from_entry() CASCADE;\n            DROP FUNCTION IF EXISTS assert_entry_balanced_from_line() CASCADE;"
)

with open('ledger/migrations/0001_initial.py', 'w') as f:
    f.write(content)
