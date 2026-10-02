# ledger/reconciliation Specification

## Purpose
Provides asynchronous background auditing of the ledger to detect and report any floating-point drift or out-of-band balance mutations.

## Requirements

### Requirement: Periodic reconciliation execution
The system MUST execute periodic asynchronous tasks to reconcile the ledger balances.

#### Scenario: Scheduled Celery beat trigger
- **WHEN** the scheduled reconciliation interval triggers
- **THEN** the system dispatches an asynchronous Celery task to perform the audit without blocking the API

### Requirement: Tenant-isolated RLS execution
The system MUST iterate through active tenants and execute the reconciliation query strictly within the Row-Level Security context of each tenant to prevent cross-tenant data exposure.

#### Scenario: Multi-tenant audit run
- **WHEN** the reconciliation engine runs across multiple tenants
- **THEN** it sets the database configuration `app.tenant_id` appropriately before each tenant's audit and records isolated `ReconciliationReport` results per tenant

### Requirement: Zero-drift tolerance check
The system MUST compare the materialized `balance` of each account against the aggregate sum of its `TransactionLine` entries according to strict double-entry normal balance rules.

#### Scenario: Perfect balance match
- **WHEN** an account's `balance` exactly matches the expected sum of lines
- **THEN** the reconciliation audit records a success for that account with zero discrepancy

#### Scenario: Balance discrepancy detected
- **WHEN** an account's `balance` differs from the sum of its lines (e.g. from out-of-band edits)
- **THEN** the reconciliation audit flags the account as anomalous and records the exact drift amount in the `ReconciliationReport`

#### Scenario: Broken query silently passing
- **WHEN** the reconciliation query returns 0 accounts checked for a tenant that actually has accounts
- **THEN** the reconciliation audit MUST strictly fail the report rather than silently passing

### Requirement: Financial invariant alerts
The system MUST persistently record the outcome of every reconciliation run, strictly marking failures when discrepancies are found.

#### Scenario: Drift alert logging
- **WHEN** any account fails the zero-drift tolerance check
- **THEN** the system creates a failed `ReconciliationReport` log, capturing the timestamp, the affected accounts, and the discrepancy magnitude

### Requirement: CLI integrity command
The system MUST provide a management command to trigger the reconciliation engine synchronously for continuous integration pipelines.

#### Scenario: Clean run in CI
- **WHEN** the management command executes and finds zero discrepancies
- **THEN** the command exits with a status code of 0

#### Scenario: Drift detected in CI
- **WHEN** the management command executes and finds balance drift or errors
- **THEN** the command exits with a non-zero status code to fail the pipeline
