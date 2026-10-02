# Proposal

## Why

DoubleLedge requires continuous assurance of its ledger's integrity to detect out-of-band modifications, floating-point drift, or software defects. While `ledger/invariants` covers synchronous database constraints (balanced entries, immutable records), we also need asynchronous balance reconciliation to ensure that materialized account balances always exactly match the aggregate sum of their transaction lines. We need Celery infrastructure to run these audits out-of-band periodically without impacting core posting throughput.

## What Changes

- Initialize a Celery application in `config/celery.py` and configure `celery_worker` and `celery_beat` to run successfully.
- Implement an optimized SQL reconciliation query to compare `accounts.balance` against `SUM(transaction_lines.amount)` partitioned by account using `LEFT JOIN` and `COALESCE`.
- Add an audit table (e.g., `ReconciliationReport`) to track audit runs, discrepancies, and financial invariant alert status. This table will include a `tenant_id` foreign key and an `accounts_checked` column.
- Add a management command `python manage.py check_ledger_integrity` (hooked into `make integrity`) that exits with a non-zero status code when drift is detected.
- Implement unit tests for drift detection and integration tests for Celery dispatch.

## Capabilities

### New Capabilities
- `ledger/reconciliation`: Asynchronous auditing of account balances against transaction line aggregates, zero-drift tolerance, and anomaly reporting.

### Modified Capabilities
<!-- None, environment/orchestration already requires healthy celery workers in its spec -->

## Impact

- **Code:** `config/celery.py`, new `ledger/services/reconciliation.py` module, new `workers/tasks.py` module (in a new `workers` app), new Django models for audit logs.
- **Dependencies:** Celery, Redis (already available via Docker).
- **Systems:** Activates the currently crashing `celery_worker` and `celery_beat` Docker containers, integrating them into the active topology.
