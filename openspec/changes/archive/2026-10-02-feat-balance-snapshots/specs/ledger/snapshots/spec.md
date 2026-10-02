# Spec Delta

## Purpose

Provides point-in-time account balance snapshot checkpoints and accelerated balance calculation combining snapshot state with un-checkpointed transaction lines.

## ADDED Requirements

### Requirement: Immutable snapshot generation
The system MUST record point-in-time balance snapshot checkpoints in an append-only table without mutating existing snapshot records.

#### Scenario: Snapshot creation
- **WHEN** a balance snapshot is recorded for an account
- **THEN** the system persists `tenant_id`, `account_id`, `up_to_entry_id`, `up_to_entry_sequence`, `balance`, `debit_total`, `credit_total`, and `line_count` as a new immutable row

#### Scenario: Monotonic sequence fence
- **WHEN** a new snapshot is generated
- **THEN** it strictly binds to a monotonic entry sequence fence (`up_to_entry_sequence`), preventing missed or double-counted transaction lines

### Requirement: Hybrid balance calculation service
The system MUST compute account balances by combining the latest snapshot at or before the requested boundary with subsequent transaction lines.

#### Scenario: Balance query with existing snapshot
- **WHEN** an account balance is queried and a valid prior snapshot exists
- **THEN** the service computes the balance as `Latest Snapshot Balance + SUM(Transaction Lines since Snapshot)`

#### Scenario: Balance query without snapshot fallback
- **WHEN** an account balance is queried and no prior snapshot exists
- **THEN** the service falls back to calculating the balance directly from all historical transaction lines

### Requirement: Asynchronous periodic snapshot worker
The system MUST periodically generate balance snapshots for accounts whose un-checkpointed transaction line count exceeds the configured threshold.

#### Scenario: Scheduled checkpoint execution
- **WHEN** the Celery periodic snapshot task runs
- **THEN** it processes accounts in tenant-isolated, bounded transactions, generating a new snapshot for accounts exceeding the line threshold

#### Scenario: Idempotent worker execution
- **WHEN** the snapshot task runs for an account whose sequence fence has already been checkpointed
- **THEN** the task skips snapshot creation without error or duplicate row insertion

### Requirement: Ledger integrity reconciliation
The system MUST provide an automated check to verify that snapshot totals strictly reconcile against historical transaction lines.

#### Scenario: Integrity audit execution
- **WHEN** `manage.py check_ledger_integrity` is executed
- **THEN** it validates that snapshot balances match the sum of historical transaction lines up to the snapshot sequence fence
