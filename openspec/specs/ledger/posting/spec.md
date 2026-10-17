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
The posting service MUST transparently recover from transient database lock or serialization failures: deadlocks (`40P01`), serialization failures (`40001`) and lock timeouts (`55P03`). Only the code that starts a transaction retries it. A posting service call that runs inside a transaction its caller opened MUST NOT retry on its own and MUST pass the error to that caller. Retries are bounded; when they run out, the last error is raised.

#### Scenario: Transient deadlock or serialization failure
- **WHEN** the underlying database raises a 40P01 or 40001 error in a posting that started its own transaction
- **THEN** the posting service retries the entire atomic block using an exponential backoff

#### Scenario: Lock timeout that clears
- **WHEN** a posting that started its own transaction waits for a row lock longer than the lock timeout, and the lock is released before its next attempt
- **THEN** the posting is retried and succeeds, and exactly one entry is written

#### Scenario: Call inside the caller's transaction
- **WHEN** a posting service call runs inside a transaction its caller opened and the database raises a deadlock
- **THEN** the call does not retry by itself, and the error reaches the caller that owns the transaction

#### Scenario: Retries exhausted
- **WHEN** every attempt of a posting fails with a transient error
- **THEN** the last error is raised to the caller and nothing from any attempt is persisted

#### Scenario: Non-transient database error
- **WHEN** a posting fails with a database error other than 40P01, 40001 or 55P03
- **THEN** it is not retried

### Requirement: Transactional outbox event emission
The posting service MUST atomically record a system event whenever a journal entry is successfully created.

#### Scenario: Successful entry posting
- **WHEN** a journal entry is posted successfully
- **THEN** the system inserts an `OutboxEvent` (e.g., `entry.posted`) within the exact same database transaction as the entry lines

#### Scenario: Rollback on failure
- **WHEN** the journal entry creation fails or rolls back
- **THEN** no `OutboxEvent` is persisted, preventing phantom webhook dispatches

### Requirement: Business-time effective_at on postings
The posting service SHALL accept an optional `effective_at` (business time) for a new journal entry. When omitted, `effective_at` MUST default to the current time. A supplied `effective_at` MUST be timezone-aware and MUST NOT be later than the current time. `posted_at` (system time) is always set by the system.

#### Scenario: effective_at omitted
- **WHEN** an entry is posted without `effective_at`
- **THEN** the entry's `effective_at` equals its posting time, as before this change

#### Scenario: Backdated entry in an open period
- **WHEN** an entry is posted with an `effective_at` in the past that is not before the tenant's cutoff
- **THEN** the entry is posted with that `effective_at` and `posted_at` is the current time

#### Scenario: Future effective_at
- **WHEN** an entry is posted with an `effective_at` later than the current time
- **THEN** the posting is rejected as `invalid_effective_at` and nothing is written

### Requirement: Closed-period rejection before write
The posting service MUST reject an entry whose `effective_at` falls in the tenant's closed period as `period_closed`, before writing any row. The database freeze is the backstop if this check is bypassed.

#### Scenario: Backdated entry into a closed period
- **WHEN** an entry is posted with `effective_at` before the tenant's cutoff
- **THEN** the posting is rejected as `period_closed`, no entry, lines, outbox event, or balance change persists, and the idempotency key is not consumed

### Requirement: Per-currency balance before write
The posting service SHALL reject a journal entry whose debits and credits are unequal within any one currency, with `unbalanced_entry`, before writing anything, even when the entry balances across all currencies combined. The commit-time database trigger remains the backstop.

#### Scenario: Balanced overall but not per currency
- **WHEN** a client posts a debit of `10.0000` to a USD account and a credit of `10.0000` to a EUR account
- **THEN** the API returns 422 `unbalanced_entry` and no entry, line or idempotency record is stored

#### Scenario: Balanced in each currency
- **WHEN** a client posts an entry whose USD lines balance and whose EUR lines balance
- **THEN** the entry is posted
