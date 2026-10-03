# ledger/posting Specification

## Purpose
Coordinates atomic, deadlock-free ledger writes at the application level.

## Requirements

### Requirement: Deterministic concurrency control
The posting service MUST prevent lock-ordering deadlocks between concurrent postings touching overlapping accounts, and MUST distribute contention for sharded accounts by locking shards instead of the master account.

#### Scenario: Concurrent opposing transfers
- **WHEN** multiple concurrent transactions attempt to transfer between the same accounts in opposing directions
- **THEN** the posting service avoids cross-locks by explicitly sorting and locking accounts in ascending primary-key order

#### Scenario: Posting to a sharded account
- **WHEN** a payload targets a sharded master account
- **THEN** the posting service deterministically or randomly maps the target to one of the master account's shards and acquires the row lock on the shard instead of the master account

### Requirement: Automatic retry of transient database errors
The posting service MUST transparently recover from transient database lock or serialization failures.

#### Scenario: Transient deadlock or serialization failure
- **WHEN** the underlying database raises a 40P01 or 40001 error
- **THEN** the posting service retries the entire atomic block using an exponential backoff

### Requirement: Transactional outbox event emission
The posting service MUST atomically record a system event whenever a journal entry is successfully created.

#### Scenario: Successful entry posting
- **WHEN** a journal entry is posted successfully
- **THEN** the system inserts an `OutboxEvent` (e.g., `entry.posted`) within the exact same database transaction as the entry lines

#### Scenario: Rollback on failure
- **WHEN** the journal entry creation fails or rolls back
- **THEN** no `OutboxEvent` is persisted, preventing phantom webhook dispatches
