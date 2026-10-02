# Tasks

## 1. Celery Infrastructure

- [x] 1.1 Create `config/celery.py` pointing to `config.settings`, configure Redis broker, and verify `celery_worker` and `celery_beat` containers boot without crashing.
- [x] 1.2 Setup `config/__init__.py` to ensure Celery is loaded when Django starts.
- [x] 1.3 Create a new `workers` Django app, add it to `INSTALLED_APPS`, and verify Celery autodiscovers it.

## 2. Reconciliation Models

- [x] 2.1 Create the `ReconciliationReport` Django model with fields for `tenant_id`, `accounts_checked`, `timestamp`, `status`, and `discrepancies` (JSONField). Ensure monetary values in the JSON are always appended as strings.
- [x] 2.2 Generate and apply the Django migration, verifying via `make migrate`.

## 3. Reconciliation Engine

- [x] 3.1 Implement the raw SQL reconciliation query inside `ledger.services.reconciliation`. Verify the engine loops through active tenants, explicitly sets RLS context (`set_config`), and uses a `REPEATABLE READ` snapshot. The query must use `LEFT JOIN transaction_lines`, `COALESCE`, and `IS DISTINCT FROM` to compute balances against double-entry rules accurately. Fail the report if `accounts_checked == 0` for a non-empty tenant. Emit a `CRITICAL` log on any drift.
- [x] 3.2 Write a unit test that forces an out-of-band `Account.balance` modification and verify the reconciliation engine flags the exact drift in a `ReconciliationReport` without raising serialization TypeErrors.
- [x] 3.3 Create a Celery `@shared_task` in `workers/tasks.py` that wraps the reconciliation engine execution. Set `ignore_result=True` and use `pg_try_advisory_lock` to prevent overlapping runs.

## 4. Integration and Tooling

- [x] 4.1 Implement `python manage.py check_ledger_integrity` and verify it runs the reconciliation engine, outputs results locally, and exits with a non-zero status code if drift or an error is detected.
- [x] 4.2 Hook the management command into `Makefile` target `make integrity` and verify `make integrity` runs correctly inside the API container.
- [x] 4.3 Configure Celery Beat schedule in Django settings to trigger the background task periodically, and write an integration test verifying task dispatch.
