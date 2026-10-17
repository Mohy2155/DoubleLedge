# Proposal

## Why

Neither retry path ever retries. `retry_on_deadlock` (`ledger/services/posting.py`) and `tenant_transaction` (`api/decorators.py`) both read the error code from `e.__cause__.pgcode`. That attribute comes from psycopg2. The project runs psycopg 3, which exposes the code as `sqlstate`, so the lookup always returns `None`. A probe that raised a server-side `40P01` through a decorated function confirmed it: the decorator re-raised on the first call.

This has three effects:
- **The posting spec is broken.** The `ledger/posting` spec requires retrying `40P01`/`40001`, and that never happens.
- **The API returns 500 instead of 503.** The `api/http-gateway` spec requires a 503 with `Retry-After` when a request hits a lock timeout. Instead, deadlocks and lock timeouts escape the exception handler and become 500s.
- **A stress test flakes.** `test_live_postings_vs_rollup_contention` fails intermittently on `main` and on feature branches, with posting/rollup deadlocks and lock timeouts that should have been retried.

Fixing the lookup alone is not enough. Retries are layered: the request's transaction contains `convert()`, which contains `post_entry`, and each layer has its own retry loop. A working lookup would multiply the attempts, up to 96 for one conversion request, each able to wait 2s on `lock_timeout`. Retrying an inner call inside a savepoint also keeps the outer transaction's locks, so the retry can walk straight back into the same conflict.

## What Changes

- Read the SQLSTATE that psycopg 3 provides, in one shared helper used by both retry paths. The retryable codes are `40P01` (deadlock), `40001` (serialization failure) and `55P03` (lock not available). The API path gains `40001`, which the posting spec already lists.
- Retry only in the code that owns the transaction. A decorated service call made inside a transaction its caller opened runs once and lets the error reach the transaction owner.
  - For API requests, `tenant_transaction` is that owner.
  - For direct callers (worker threads, scripts, tests), the outermost service call is.
- Keep the attempt limits as they are: the API retries 3 times, `post_entry` 5 times, and holds and conversions 3 times. Log each retry.
- When the API's retries run out on a transient error, return 503 `lock_timeout` with `Retry-After: 1`, as the gateway spec already requires, instead of a 500.
- Correct the README's concurrency section:
  - It claims the retry count is exposed as a metric. No such metric exists.
  - It implies a lock timeout fails fast without retrying.
- No API shape, schema or migration changes.

## Capabilities

### New Capabilities
None.

### Modified Capabilities
- `ledger/posting`: "Automatic retry of transient database errors" now names the retryable codes, says retries happen only where the transaction starts, and covers what happens when attempts run out.
- `api/http-gateway`: "Error Mapping" now says the API retries a request's transaction on deadlock, serialization failure or lock timeout before returning 503. It also covers a transient failure that recovers on retry.

## Impact

- **Code:**
  - `ledger/services/posting.py`: the retry helper and `retry_on_deadlock`
  - `api/decorators.py`: `tenant_transaction`
  - Callers keep their decorators; the hold and FX services and the rollup worker need no edits.
- **Behavior:**
  - API requests that hit a deadlock or lock timeout are retried up to 3 times, about 8.7s in the worst case. Then they get a 503 instead of a 500.
  - The rollup worker keeps running each tenant in one transaction. A deadlock there still fails that tenant's run, which the next scheduled run picks up, as it does today.
- **Tests:**
  - new tests for the retry decorators, using real server-raised SQLSTATEs and a real lock wait
  - API tests for retried requests and the 503 mapping
  - repeated runs of the sharding stress test to confirm the flake is gone
- **Docs:** README Core Design "Concurrency control" bullets.
