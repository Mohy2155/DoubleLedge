# ledger/idempotency Specification

## Purpose
Guarantees distributed-safe idempotency for ledger write operations that survives crashes and races.

## Requirements

### Requirement: Atomic idempotency key insertion
The system MUST guarantee that an idempotency key and its resulting state changes exist exactly together.

#### Scenario: Successful idempotent write
- **WHEN** a mutating request provides an `Idempotency-Key`
- **THEN** the key and the resulting ledger entries are inserted in the exact same database transaction

#### Scenario: Mid-request process crash
- **WHEN** the process dies before the ledger transaction commits
- **THEN** the idempotency key is rolled back with the transaction and the client can safely retry

### Requirement: Concurrent duplicate prevention
The system MUST prevent concurrent requests with the same key from processing simultaneously.

#### Scenario: Concurrent duplicate keys
- **WHEN** a second request arrives with an identical key while the first is still processing
- **THEN** the second request blocks on the database unique index until the first completes, then replays the stored response

### Requirement: Idempotency key payload validation
The system MUST reject an idempotency key if the request payload differs from the original request.

#### Scenario: Key reuse with different payload
- **WHEN** a request arrives with an existing key but a different request hash
- **THEN** the system rejects it with an idempotency_key_reuse error (422)
