with open('ledger/migrations/0001_initial.py', 'r') as f:
    content = f.read()

content = content.replace('RETURNS trigger AS \n            DECLARE', 'RETURNS trigger AS $$\n            DECLARE')
content = content.replace('DROP FUNCTION IF EXISTS assert_entry_balanced_from_entry() CASCADE;\\n            DROP FUNCTION \nIF EXISTS assert_entry_balanced_from_line() CASCADE;', 'DROP FUNCTION IF EXISTS assert_entry_balanced_from_entry() CASCADE;\n            DROP FUNCTION IF EXISTS assert_entry_balanced_from_line() CASCADE;')

with open('ledger/migrations/0001_initial.py', 'w') as f:
    f.write(content)
