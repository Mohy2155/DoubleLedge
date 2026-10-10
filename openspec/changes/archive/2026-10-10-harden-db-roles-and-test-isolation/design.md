# Design

## 1. Database Roles & Django Configuration
- **`default` (Runtime)**: The standard connection. In production and Celery, it connects using `ledger_runtime` (a role with `NOSUPERUSER NOBYPASSRLS NOCREATEDB` that owns nothing).
- **Startup Guard**: A guard runs on app startup to assert that `current_user` is not a superuser and cannot bypass RLS. This prevents accidental deployment with superuser credentials.

Role creation for `ledger_runtime` and `ledger_tenant_lister` will be handled in provisioning (e.g. Compose init scripts) using passwords from the environment.

## 2. Test Execution Context and DB Lifecycle (Spike v2)
- **Previous Spike Failures**: Spike v1 failed because swapping `default` credentials to `ledger_runtime` caused `transaction=True` `flush` to crash (`TRUNCATE` forbidden), DB destruction to crash (not DB owner), and test runners forbade queries to a secondary `migrator` alias.
- **Spike v2 Design**: The test DB connection (`default`) remains the superuser to naturally handle creation, destruction, and `flush`. An `autouse` fixture will execute `SET ROLE ledger_runtime` at the start of every test, and `RESET ROLE` before teardown.
- **Test Privileges**: Tenant fixtures and DDL tests will use `RESET ROLE ... finally SET ROLE` to perform privileged operations. A single smoke test will connect directly as `ledger_runtime` to verify connection-level constraints. (Fallback: Migrator-run teardown if `SET ROLE` proves insufficient).

## 3. Tenant Fixtures and RLS Bypass Design
- **Tenant Enumeration**: We will implement a `SECURITY DEFINER list_tenant_ids()` function in a migration. It will be owned by a dedicated `ledger_tenant_lister` role (`BYPASSRLS`). Its `search_path` will be pinned. `GRANT EXECUTE` will be guarded if the role is missing.
- **Sweep Canary**: `for_each_tenant` will return `{listed, processed, failed}`. We will add alerting on `processed == 0` or `failed > 0`. A test will be added where listing returns zero.
- **Tenant Creation Under RLS**: To create a tenant under RLS, we must `set_config` before `INSERT` with a client-generated UUID.
- **Grants**: Grants are per-database. We will re-apply them after the final migration or use `ALTER DEFAULT PRIVILEGES`. `REVOKE CREATE ON SCHEMA public FROM PUBLIC`.
