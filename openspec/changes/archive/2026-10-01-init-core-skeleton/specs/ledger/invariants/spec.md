# Spec Delta

## Purpose

Enforces core accounting invariants at the database level to guarantee ledger correctness regardless of application state.

## ADDED Requirements

### Requirement: Balanced journal entries
The database MUST reject any journal entry where the sum of debits does not equal the sum of credits within each currency.

#### Scenario: Submitting balanced entry
- **WHEN** an entry is inserted with equal debits and credits in a currency
- **THEN** the transaction commits successfully

#### Scenario: Submitting unbalanced entry
- **WHEN** an entry is inserted with unequal debits and credits in a currency
- **THEN** the transaction is rejected at commit time by a deferred trigger

#### Scenario: Submitting entry with fewer than two lines
- **WHEN** an entry is inserted with zero or one line
- **THEN** the transaction is rejected at commit time by a deferred trigger

### Requirement: Immutable ledger entries
The database MUST reject any modification or deletion of posted journal entries or transaction lines.

#### Scenario: Attempting to update a journal entry
- **WHEN** an UPDATE is issued against the journal_entries table
- **THEN** a trigger raises an exception and aborts the transaction

#### Scenario: Attempting to delete a transaction line
- **WHEN** a DELETE is issued against the transaction_lines table
- **THEN** a trigger raises an exception and aborts the transaction
