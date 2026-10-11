# Tasks
## 1. API
- [x] 1.1 Accounts list (top-level only, aggregate balances).
- [x] 1.2 Holds list (newest first, optional status filter).
- [x] 1.3 `create_tenant --tenant_id` mints a token only.
## 2. UI
- [x] 2.1 `/demo/` page: token box, accounts, statement, transfer form.
- [x] 2.2 Holds: list, authorize, capture, void.
## 3. Verification
- [x] 3.1 Tests: list endpoints are tenant-scoped, never return shard
      ids, and `/demo/` returns 200.
- [x] 3.2 Full non-redteam suite green with `--create-db`.
- [x] 3.3 Manual: I will click through the UI myself.
## 4. Docs
- [x] 4.1 README section: how to open the demo.

