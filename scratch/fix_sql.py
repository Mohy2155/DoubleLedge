import re

with open('ledger/migrations/0001_initial.py', 'r') as f:
    content = f.read()

content = content.replace("RETURNS trigger AS \n", "RETURNS trigger AS $$\n")
content = content.replace("\n             LANGUAGE plpgsql;", "\n            $$ LANGUAGE plpgsql;")

# Remove duplicate trigger declaration
content = re.sub(
    r"CREATE CONSTRAINT TRIGGER trg_entry_balanced_entry\s+AFTER INSERT ON journal_entries\s+DEFERRABLE INITIALLY DEFERRED\s+FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced\(\);\s*",
    "",
    content
)

with open('ledger/migrations/0001_initial.py', 'w') as f:
    f.write(content)
