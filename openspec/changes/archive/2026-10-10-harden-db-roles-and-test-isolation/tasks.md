# Tasks

## 1. Test DB Spike v2
- [x] 1.1 Spike test-DB creation v2: Status: v1 failed; v2.4 passed (green)

## 2. Canary and Tenant Enumeration (Pre-Cutover)
- [x] 2.1 Trace tenant resolution on API requests and any pre-context queries against tenants/auth tables.
- [x] 2.2 Verify `workers/tasks.py` enumerates tenants and check how `reconciliation.py` sets context.
- [x] 2.3 Implement `SECURITY DEFINER list_tenant_ids()` in a migration. Provision a dedicated `ledger_tenant_lister` role (`BYPASSRLS`, owns only this function). Refactor `for_each_tenant` to return `{listed, processed, failed}` and use the helper. Fix `create_sharded_account.py`. Document that tenant creation under RLS requires client-generated UUID and `set_config` before `INSERT` (and that `Tenant.objects.create` in the fixture runs under `RESET ROLE`).
- [x] 2.4 Add a sweep canary: alert on `processed==0` or `failed>0`. Add a test where listing returns zero.
- [x] 2.5 Grep to confirm which tables the application actually `UPDATE`s/`DELETE`s, and refine `ledger_runtime` grants to absolute least privilege.

## 3. Cutover Gate and CI
- [x] 3.1 Cutover Gate: Do not cutover prod until 2.x is done, the suite passes under `ledger_runtime` (via `SET ROLE`), and all findings are triaged.
- [x] 3.2 Redesign `settings.py`: Keep `default` alias simple. Add a startup guard outside tests that fails if `default` resolves to a superuser or `BYPASSRLS`. Configure Celery worker/beat credentials to use `ledger_runtime`.
- [x] 3.3 Ensure grants are per-database and re-applied via session-scoped `GRANT ... ON ALL TABLES` after migrations. `REVOKE CREATE ON SCHEMA public FROM PUBLIC`.

## Future Work & Deferred Tasks
> *Note: Per design agreement, tasks 3.4 and 4.x are deferred to future hardening work. The test suite operates with opt-in `@pytest.mark.runtime_role` for RLS/role-specific tests while general tests run as DB owner.*

- [ ] 3.4 Extend CI test (`tests/test_db_roles.py`) asserting no owned objects, no superuser/BYPASSRLS, no `CREATE` on public, and cannot `DISABLE TRIGGER`.
- [ ] 4.1 Inventory tests needing DDL/superuser, ensure they use `RESET ROLE` with `finally SET ROLE`.
- [ ] 4.2 Run the full test suite under `ledger_runtime` via the `autouse` fixture. Log all failures as findings for the subsequent remediation change.
- [ ] 4.3 Draft rollout notes for prod (assumed superuser until verified), Celery, and CI.
