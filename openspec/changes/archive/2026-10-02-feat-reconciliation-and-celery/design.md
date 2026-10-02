# Design

## Context

The core posting engine is already live and tested, safely catching deadlocks and enforcing double-entry rules. The environment specifies Celery components (`celery_worker`, `celery_beat`), but they crash immediately because `config.celery` is uninitialized. The ledger requires continuous drift detection to fulfill reliability requirements without slowing down the hot synchronous API paths.

## Goals / Non-Goals

**Goals:**
- Fix the Celery container boot loops by initializing a production-ready Celery app in Django.
- Execute a highly optimized raw SQL reconciliation query across all accounts asynchronously.
- Write anomalies to a new `ReconciliationReport` Django model.
- Expose the reconciliation trigger via a Django management command for manual/CI testing.

**Non-Goals:**
- Automated remediation/auto-fixing of balance drift. Drift must be manually audited and repaired.
- Reconciliation of external payment gateways (this is purely internal double-entry integrity).

## Decisions

### 1. Celery Configuration
**Decision:** Initialize `config.celery.app` explicitly pointing to Django's settings, using Redis as broker. Celery tasks will be defined in `workers/tasks.py` (within a new `workers` Django app) with `ignore_result=True` to prevent Redis bloat. To prevent overlapping reconciliation runs on the same tenant, tasks will use `pg_try_advisory_lock`.
**Rationale:** Keeps the codebase cleanly separated. Disabling results ensures Redis acts only as a broker, since the `ReconciliationReport` table serves as the system of record. Advisory locks safely control concurrency at the database level.

### 2. Reconciliation Query Logic
**Decision:** Execute reconciliation per-tenant inside a single raw SQL query rather than fetching entries into Python memory. The engine will loop through active tenants, open an atomic transaction, set the RLS context (`SELECT set_config('app.tenant_id', %s, true)`), and execute the query within that scope.
The query will `SELECT` from `accounts` and `LEFT JOIN transaction_lines`, aggregating with `COALESCE(SUM(...), 0)` to detect accounts that have had their balances modified but have no lines. It will use standard double-entry rules and `IS DISTINCT FROM` for safe balance comparisons.
**Rationale:** `SUM(amount)` over millions of lines in Python is impossibly slow; Postgres computes this instantly. A `LEFT JOIN` guarantees we catch zero-line accounts with injected balances. Explicit RLS context injection ensures the worker complies with the system's strict row-level security.
**Alternative:** Django ORM aggregations. However, doing a massive `GROUP BY` with conditional logic (case statements on `account.type` and `line.direction`) is extremely complex in ORM and produces inefficient SQL. A raw Postgres query utilizing `CASE WHEN` is much faster and cleaner.

### 3. Reporting Model
**Decision:** Create a `ReconciliationReport` model capturing: `id`, `tenant_id`, `timestamp`, `status` (CLEAN, DRIFT_DETECTED), `accounts_checked` (integer), and a `JSONField` `discrepancies` storing `{ account_id, expected_balance, actual_balance, difference }`. All monetary values in the JSON MUST be stored as strings to prevent Python `Decimal` to float coercion/`TypeError` during serialization. If `accounts_checked == 0` for a tenant that has accounts, the report will be marked as a failure. Any drift will also emit a `CRITICAL` log.
**Rationale:** JSON limits schema thrashing while allowing structured audit logs. String monetary values preserve precision safely. Checking `accounts_checked` prevents a broken query from silently passing.

## Risks / Trade-offs

- **Risk:** Reconciliation locks tables during execution, blocking the core posting engine.
  - **Mitigation:** The SQL query will strictly be read-only (`SELECT`) without `FOR UPDATE`. The transaction will use `REPEATABLE READ` isolation to ensure a completely consistent snapshot across the multi-statement RLS setup and query, guaranteeing we don't read partial transactions.
- **Risk:** Uncommitted transactions cause false positive drift.
  - **Mitigation:** By design, Postgres read committed isolation prevents dirty reads; the reconciliation query only sees fully committed balances and fully committed lines.
