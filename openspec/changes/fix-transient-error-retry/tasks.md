# Tasks

## 1. Service-level retry

- [ ] 1.1 Write `tests/test_transient_retry.py` (`transaction=True`) before changing code. Each case uses a decorated function that raises a real server error with `RAISE EXCEPTION ... USING ERRCODE`. Confirm the transient cases fail against the current decorator. Cases:
  - for each of `40P01`, `40001` and `55P03` raised on the first call, the function is retried and returns, and a row written by the failed attempt is not persisted
  - a non-transient `OperationalError` (`57014`) is raised after one call
  - an error raised on every attempt is re-raised after `max_retries + 1` calls, and nothing persists
  - inside an outer `transaction.atomic()`, the function is called exactly once and the error propagates
- [ ] 1.2 Add `TRANSIENT_SQLSTATES` and `transient_sqlstate()` to `ledger/services/posting.py`. Rewrite `retry_on_deadlock` to read `sqlstate`, retry only when no transaction is open (design D2), and log each retry at WARNING with the SQLSTATE and attempt number. Verify that the 1.1 tests pass, and that `tests/test_fx_conversions.py`, `tests/test_holds*.py`, `tests/test_deadlocks.py` and `tests/test_sharding*.py` still pass.
- [ ] 1.3 Add a real-contention test to `tests/test_transient_retry.py`. A second connection holds `FOR UPDATE` on the source account for about 2.5s, longer than the 2s `lock_timeout`, then commits. Verify that `post_entry`, called with no transaction open, returns 201 with exactly one entry, takes longer than the lock timeout, and logs one `55P03` retry.

## 2. API retry and error mapping

- [ ] 2.1 Write `tests/test_transient_retry_api.py` (`transaction=True`, JWT test keys) before changing code. Confirm the cases fail against the current `tenant_transaction`. Cases:
  - `POST /api/v1/entries/` whose first attempt raises a server `40P01` after the entry is written returns 201, with exactly one entry and one idempotency key stored
  - for each of `40P01`, `40001` and `55P03` raised on every attempt, the API returns 503 with code `lock_timeout` and `Retry-After: 1`, after 4 attempts
  - a non-transient `OperationalError` is not turned into a 503 and is not retried
  - `POST /api/v1/fx-conversions/` whose first `post_entry` attempt deadlocks is retried as a whole request and returns 201, with `post_entry` called exactly twice
- [ ] 2.2 Update `tenant_transaction` in `api/decorators.py` to use `transient_sqlstate()`, retry only when it owns the transaction, and raise `LockTimeout` when it stops on a transient error (design D4). Verify that the 2.1 tests pass, along with `tests/test_api_auth.py`, `tests/test_api_endpoints.py` and `tests/test_fx_conversions_api.py`.
- [ ] 2.3 Correct the README:
  - "Deadlock Mitigation": the transaction owner retries `40P01`, `40001` and `55P03` with bounded, jittered backoff and logs each retry; there is no metric, and nested calls do not retry on their own
  - "Bounded waits": each attempt waits at most 2s, and the API returns 503 `Retry-After` once its retries run out
  - the `503 lock_timeout` row in the error contract, if it needs the same wording

  Verify that every code, setting and name the edited text mentions exists in the code.

## 3. Integration

- [ ] 3.1 Run the full suite (`docker compose run --rm --no-deps owner pytest --create-db -p no:cacheprovider --ignore-glob='*redteam*'`). Verify that everything passes: the 222 baseline tests plus the new ones.
- [ ] 3.2 Run the full suite 20 times with log capture. Verify that `test_live_postings_vs_rollup_contention` does not fail in any run, and that the WARNING retry lines show transient errors were retried during the stress tests. Before the fix, it failed 1 of 30 runs on `main` and 2 of 6 on `feat/fx-conversions`.
- [ ] 3.3 Run `make stress` and `make integrity` (`docker compose run --rm --no-deps owner python scripts/demo.py` and `... python manage.py check_ledger_integrity`). Verify that the demo reports all transfers posted and the integrity check passes with 0 discrepancies.

## Workflow follow-up

- Commit on `fix/transient-error-retry` (no attribution), push, and open a PR for the user to review and merge.
- After the merge, archive the change (`/opsx:archive fix-transient-error-retry`).
