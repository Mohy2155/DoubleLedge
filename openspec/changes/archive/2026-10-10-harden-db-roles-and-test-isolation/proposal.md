# Proposal

## Why
Our recent red-team threat modeling revealed that the application connects to the database as `postgres` (a superuser with `BYPASSRLS` privileges). This completely bypasses the correctly configured Row-Level Security (RLS) on tenant tables during runtime. To guarantee multi-tenant data isolation and ensure that RLS actually works in production, the application must connect with a restricted role. This is the highest priority fix ahead of other threat-model remediations.

## What Changes
- **Restricted Runtime Role (`default`)**: The application connects using a restricted PostgreSQL role (`ledger_runtime`) that is `NOSUPERUSER`, `NOBYPASSRLS`, `NOCREATEDB`, owns nothing, and holds only least-privilege DML grants. 
- **Tenant Enumeration**: Implement a `SECURITY DEFINER list_tenant_ids()` function owned by a new `ledger_tenant_lister` role so that background sweeps do not silently fail.
- **Tenant Context Resolution**: Trace how API requests resolve tenants and ensure no pre-context queries break under RLS.
- **Test Suite Execution (Spike v2)**: Instead of a separate `migrator` alias, the test suite connection (`default`) stays as a superuser to allow DB creation and flushing, but uses an `autouse` fixture to `SET ROLE ledger_runtime` during test execution and `RESET ROLE` before teardown.
- **Startup Guard**: Add a startup check that fails the application if the `default` connection resolves to a superuser or `BYPASSRLS` role outside of tests.

## Capabilities
*Note: `skip_specs` is true because this is a systemic infrastructure change.*

## Impact
- **Security**: RLS will correctly enforce multi-tenant isolation at runtime.
- **Test Infrastructure**: Tests will expose actual production-like permission/RLS failures via `SET ROLE`.
