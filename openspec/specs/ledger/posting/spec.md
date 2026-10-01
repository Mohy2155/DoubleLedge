# ledger/posting Specification

## Purpose
Coordinates atomic, deadlock-free ledger writes at the application level.

## Requirements

### Requirement: Deterministic concurrency control
The posting service MUST prevent lock-ordering deadlocks between concurrent postings touching overlapping accounts.

#### Scenario: Concurrent opposing transfers
- **WHEN** multiple concurrent transactions attempt to transfer between the same accounts in opposing directions
- **THEN** the posting service avoids cross-locks by explicitly sorting and locking accounts in ascending primary-key order

### Requirement: Automatic retry of transient database errors
The posting service MUST transparently recover from transient database lock or serialization failures.

#### Scenario: Transient deadlock or serialization failure
- **WHEN** the underlying database raises a 40P01 or 40001 error
- **THEN** the posting service retries the entire atomic block using an exponential backoff
