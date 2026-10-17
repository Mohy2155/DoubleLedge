# Spec Delta

## MODIFIED Requirements

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
