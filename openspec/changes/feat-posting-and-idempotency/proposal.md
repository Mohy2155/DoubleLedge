# Proposal

## Why

The database schema and triggers currently enforce Doubleline's core invariants, but we lack the application-level service to safely construct and commit these entries. This change implements the core `post_entry` service, bringing deterministic row locking and robust idempotency to the ledger to prevent deadlocks and duplicate processing under concurrent load.

## What Changes

- Create the core Django models mapping precisely to the database tables (`Tenant`, `Account`, `JournalEntry`, `TransactionLine`, `IdempotencyKey`, `OutboxEvent`).
- Implement the `post_entry` transaction service using explicit deterministic row-locking to prevent deadlocks.
- Implement atomic idempotency key insertion within the posting transaction to guarantee that a key never exists without its corresponding entry, correctly replaying stored responses for duplicates.
- Implement an automatic retry mechanism for Postgres serialization errors and deadlocks (`40P01`, `40001`).

## Capabilities

### New Capabilities
- `ledger/posting`: The core application service that coordinates atomic, deadlock-free ledger writes.
- `ledger/idempotency`: Distributed-safe idempotency guarantees for ledger write operations.

### Modified Capabilities

- 

## Impact

- Scaffolds the foundational Django models that all future APIs and workers will consume.
- Introduces `ledger/services/posting.py` as the only allowed mutation path for journal entries in the application.
