# Design

## Context

See proposal.md (Why) for the bug. The retry machinery today:

- **`retry_on_deadlock`** (`ledger/services/posting.py`) wraps `post_entry` (5 retries), the hold services (3 retries) and `convert` (3 retries). Each also carries `@transaction.atomic`. When one is called inside another transaction, its atomic block is a savepoint.
- **`tenant_transaction`** (`api/decorators.py`) opens each API request's transaction and sets `app.tenant_id` and `lock_timeout = '2s'`. It retries 3 times and maps exhausted retries to `LockTimeout` (503). `ATOMIC_REQUESTS` is off, so in production this is always the outermost transaction.
- **Callers that already have a transaction open:**
  - the views, through `tenant_transaction`
  - `convert` and `capture_hold`, which call `post_entry`
  - the shard rollup, which runs each tenant inside `for_each_tenant`'s transaction
  - `seed_demo`
- **Callers with no transaction open:**
  - `scripts/demo.py` worker threads
  - the stress and race tests, which call services directly under `transaction=True`
- **Side effects outside the database:** the only one is the Redis rate limiter. DRF runs it before the handler, outside `tenant_transaction`. Outbox events are written in the transaction. So re-running a request's handler has no side effects outside the database.

Django wraps driver errors in `django.db.OperationalError` and chains the psycopg 3 exception as `__cause__`. That exception carries the server's code in `sqlstate`.

## Goals / Non-Goals

**Goals:**
- Transient errors are retried exactly once per transaction, by the code that started it.
- A request that keeps hitting transient errors gets the gateway's 503, never a 500.
- One definition of "transient", shared by both paths.

**Non-Goals:**
- Metrics for retries. Retries are logged; the README stops claiming a metric.
- Changing the attempt limits, the backoff formula or `lock_timeout`.
- Retrying the rollup worker or other periodic jobs inside their run. A failed tenant run is picked up by the next scheduled run, as today.
- Restructuring the rollup to avoid the posting/rollup deadlock itself. Retries absorb it. Avoiding it would need a different locking scheme for rollups.

## Decisions

### D1. Classify by `sqlstate` on the chained driver error

`transient_sqlstate(exc)` in `ledger/services/posting.py` returns `exc.__cause__.sqlstate` when it is one of `TRANSIENT_SQLSTATES = ('40P01', '40001', '55P03')`, and `None` otherwise. Both decorators use it, and `api/decorators.py` already imports from `ledger.services`.

Alternatives:
- **Match psycopg exception classes** (`DeadlockDetected`, `SerializationFailure`, `LockNotAvailable`). This works, but it ties the check to the driver's class tree, while the specs and Postgres documentation talk in SQLSTATEs.
- **Read `pgcode` with a `sqlstate` fallback.** The project has no psycopg2, so the fallback would be dead code that hides the next mismatch.

The API path gains `40001`. Under `READ COMMITTED` it is rare, but the posting spec lists it, and one shared list keeps the two paths from drifting apart again.

### D2. Retry only in the code that owns the transaction

Each wrapper checks `connection.in_atomic_block` when it is called, before entering the wrapped function.
- **If a transaction is already open,** the wrapper calls the function once and lets any error propagate.
- **Otherwise** the wrapper owns the transaction and runs the retry loop.

`tenant_transaction` makes the same check. In production it never finds an open transaction. Under a non-transactional test it runs the handler once, but it still maps a transient error to 503.

Why not let inner layers retry inside their savepoints:
- **Rolling back to a savepoint keeps the outer transaction's locks**, such as the idempotency key row or the hold row. The retry then queues behind the same holder or rebuilds the same cycle.
- **Attempts multiply across layers.** A conversion request could make 4 × 4 × 6 = 96 attempts, each able to wait 2s.
- **Re-running only part of a transaction** is harder to reason about than re-running all of it. A whole-transaction retry restarts from a clean state: a fresh idempotency insert, fresh locks, fresh balances.

Alternatives:
- **Remove the decorators from nested services.** `post_entry`, `convert` and the hold services are also called with no transaction open, by scripts, tests and future callers, and those calls need retries.
- **A thread-local "retry scope" counter.** It is more machinery for the same answer, since Django already tracks `in_atomic_block` per connection.

### D3. Keep the limits; log every retry

The attempt limits and backoff formulas stay as they are: `base_delay * 2**n` plus up to 0.1s of jitter.
- **API:** 3 retries. The worst case is about 4 × 2s of lock waits plus 0.7s of backoff, plus jitter.
- **Direct callers:** `post_entry` retries up to 5 times, holds and conversions 3.

`retry_on_deadlock` gains a WARNING log with the SQLSTATE and attempt number, matching what `tenant_transaction` already logs.

The alternative was to stop retrying `55P03` in the API so lock timeouts fail fast, which is what the README says. We rejected it. Both decorators' code and docstrings intend to retry lock timeouts, and the stress test expects contended postings to land. When contention persists, the client still gets a 503 with `Retry-After`. The README is corrected instead.

### D4. API mapping of transient errors

When `tenant_transaction` stops on a transient error, because retries ran out or it was not the owner, it raises `LockTimeout`: 503 with code `lock_timeout` and `Retry-After: 1`. This now applies to all three SQLSTATEs. Other `OperationalError`s are re-raised unchanged.

### D5. Testing with real server errors

- **Classification and the owner rule:** a decorated function runs `RAISE EXCEPTION ... USING ERRCODE = '<code>'`. That is a genuine server error with the target SQLSTATE, so it goes through the same Django wrapping and psycopg class as a real deadlock.
- **One end-to-end test with real contention:** a second connection holds `FOR UPDATE` on an account for longer than the 2s `lock_timeout` and then releases it. The posting's first attempt times out and the retry succeeds.
- **Real deadlocks** are not forced deterministically, because Postgres chooses the victim. The sharding stress test covers them statistically.
- **API tests** use `transaction=True`, so `tenant_transaction` owns the transaction as it does in production.

## Risks / Trade-offs

- **[Worst-case request latency rises to about 9s under persistent contention]** → Attempts stay bounded at 4 for the API. Clients get `Retry-After`. Before this change the same requests failed with 500s.
- **[Callers that already have a transaction open never retry]** → They never had working retries. Their owner retries: the API request, or the next scheduled run for the rollup.
- **[Retry tests silently stop retrying if run inside an outer atomic block]** → Retry tests are marked `transaction=True`. One test also asserts the opposite: inside an outer block, the function is called exactly once.
- **[The stress test could still flake if a real cycle repeats across all attempts]** → It gets 5 attempts with growing jittered backoff. Task 3.2 runs it repeatedly to measure the rate.

## Migration Plan

Code only: no schema, API shape or settings changes. Deploy normally. Roll back by reverting the commit, which restores the current behavior of never retrying.
