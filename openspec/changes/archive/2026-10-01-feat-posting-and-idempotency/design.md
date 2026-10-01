# Design

## Context

The database schema and invariants are fully established and currently enforced at the PostgreSQL level. We are building the Django integration layer that coordinates the transaction blocks, locks the rows, and commits the entries. We cannot use Redis locks or global unique constraint tricks, but must rely entirely on PostgreSQL's row-level locking (`SELECT ... FOR UPDATE`) and `ON CONFLICT` insertions.

## Goals / Non-Goals

**Goals:**
- Provide Python classes (`Django Models`) for `Tenant`, `Account`, `JournalEntry`, `TransactionLine`, `IdempotencyKey`, and `OutboxEvent` aligned with the existing schema.
- Expose a `post_entry` atomic service that strictly adheres to the database-driven concurrency approach.
- Ensure the idempotency key and ledger write cannot be separated by an application crash.

**Non-Goals:**
- HTTP API implementation (to be handled in a separate change).
- Celery webhook/outbox relay logic (to be handled in a separate change).

## Decisions

**Decision 1: `managed=False` vs standard migrations for existing tables**
We will declare the Django models normally, but we will NOT generate new migrations since `0001_initial.py` already contains the `RunSQL` creating the schema. The models will mirror the existing raw SQL schema precisely.

**Decision 2: Deterministic row locking**
To avoid classical deadlock scenarios when two concurrent threads transfer money between the same two accounts in opposing directions (A→B vs B→A), the posting service will always sort the `account_ids` in ascending order and lock them via `Account.objects.select_for_update().filter(id__in=account_ids).order_by("id")`. 

**Decision 3: Idempotency insertion in the same transaction block**
We will wrap the entire `post_entry` logic in `@transaction.atomic`.
1. Insert `IdempotencyKey` inside the block.
2. Catch `IntegrityError` (UniqueViolation) if it exists.
3. If it exists, read the row and handle replay.
Wait, `IntegrityError` in a Postgres transaction normally aborts the transaction. To safely catch a unique constraint violation without rolling back the *entire* atomic block, we will wrap the insertion in a nested `transaction.atomic` (savepoint), or use `ON CONFLICT DO NOTHING`. Using `bulk_create(ignore_conflicts=True)` or raw SQL `INSERT ... ON CONFLICT DO NOTHING RETURNING *` is safer and avoids transaction aborts.

## Risks / Trade-offs

- **Risk: Hot-account serialization bottleneck** → Mitigation: Acknowledged limitation of row locks. A `lock_timeout` will be applied to prevent queue pile-ups, instantly returning a 503 instead of backing up connections.
- **Risk: Unhandled Deadlocks from other FK constraints** → Mitigation: Django will catch `OperationalError` and inspect the `pgcode`. We will retry on `40P01` (deadlock_detected) and `40001` (serialization_failure) with exponential backoff.
