# Spec Delta

## MODIFIED Requirements

### Requirement: Zero-drift tolerance check
The system MUST compare the materialized `balance` of each account against the aggregate sum of its `TransactionLine` entries according to strict double-entry normal balance rules, ensuring that master account balances correctly reflect their own lines without double-counting shards.

#### Scenario: Perfect balance match
- **WHEN** an account's `balance` exactly matches the expected sum of lines
- **THEN** the reconciliation audit records a success for that account with zero discrepancy

#### Scenario: Balance discrepancy detected
- **WHEN** an account's `balance` differs from the sum of its lines (e.g. from out-of-band edits)
- **THEN** the reconciliation audit flags the account as anomalous and records the exact drift amount in the `ReconciliationReport`

#### Scenario: Broken query silently passing
- **WHEN** the reconciliation query returns 0 accounts checked for a tenant that actually has accounts
- **THEN** the reconciliation audit MUST strictly fail the report rather than silently passing

#### Scenario: Sharded account reconciliation
- **WHEN** reconciling a sharded master account and its shards
- **THEN** the reconciliation query independently verifies the master account's lines against its balance, and each shard's lines against its balance, without conflating them
