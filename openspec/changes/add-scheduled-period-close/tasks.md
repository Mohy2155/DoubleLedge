# Tasks

## 1. Schema & database enforcement

- [x] 1.1 Create `ledger/migrations/0013_period_close.py` (`RunSQL`, depends on `0012_runtime_grants`). It must contain: the `period_closes` table and index (design D1); the `tenants.period_close_grace_days` column with `CHECK (BETWEEN 0 AND 27)`; RLS enable/force plus the fail-closed `tenant_isolation_period_closes` policy; `block_modification()` triggers for `UPDATE`/`DELETE`/`TRUNCATE`; `assert_period_open()` with `BEFORE INSERT` triggers on `journal_entries` and `transaction_lines`; `assert_period_close_valid()` with a `BEFORE INSERT` trigger on `period_closes`, using the shared/exclusive advisory lock protocol (D3/D4); and a conditional `GRANT SELECT, INSERT ON period_closes TO ledger_runtime`. The reverse SQL drops all of it. Verify that `manage.py migrate` succeeds and that migrating back to 0012 and forward again works.
- [x] 1.2 Add the `PeriodClose` model (`managed=False`) and `Tenant.period_close_grace_days` to `ledger/models.py`, and a `CreateModel` state operation in 0013. Verify that the model imports and queries, and that `makemigrations --check --dry-run` reports no new drift compared with the baseline.
- [x] 1.3 Write `tests/test_period_close_db.py` covering the database backstop with raw SQL: an entry insert before the cutoff is rejected (`period_closed`); an entry exactly at the cutoff is accepted; a line insert for an old entry in a closed period is rejected; a non-monotonic or future close is rejected (`invalid_period_cutoff`); `UPDATE`/`DELETE` on `period_closes` are rejected; a cutoff for tenant A does not affect tenant B; and RLS hides tenant A's closes from tenant B under the `runtime_role` marker. Verify that `pytest tests/test_period_close_db.py` passes.
- [x] 1.4 Write a concurrency test that runs two threads with separate DB connections. A posting inserts a backdated entry and holds its transaction open, then a close starts. Assert that the close blocks until the posting commits. Then assert the reverse order: while a close transaction is open, a backdated posting blocks and is then rejected as `period_closed`. Verify that the test passes reliably in 10 consecutive runs.

## 2. Period service & posting integration

- [x] 2.1 Create `ledger/services/periods.py` with `PeriodCloseError`, `get_cutoff(tenant_id)`, `close_period(tenant, cutoff, source)` (pre-validation plus mapping of the trigger's `IntegrityError`), and `scheduled_cutoff(now, grace_days)`. Verify with unit tests in `tests/test_periods.py` for `scheduled_cutoff` (grace elapsed, grace not elapsed, grace 0, the February/27-day edge, the December→January year rollover) and for `close_period` validation.
- [x] 2.2 Extend `post_entry` to accept an optional `effective_at`: reject a future value (`invalid_effective_at`) and a value before the cutoff (`period_closed`), both after the idempotency lookup and before any write. Pass it to `JournalEntry.objects.create`, and add UTC ISO 8601 `effective_at` to `response_body`, which also makes it part of the outbox payload. Verify with tests in `tests/test_periods.py`: a backdated post into an open period succeeds and keeps its `effective_at`; a post into a closed period writes nothing (no entry, outbox event, or balance change) and leaves the idempotency key unconsumed; a replay of a pre-close success still returns 201; reversing an entry from a closed period succeeds and is dated now. Also verify that the existing posting and webhook tests still pass.

## 3. HTTP API

- [x] 3.1 Add the `effective_at` input field (rejects naive timestamps) to `JournalEntryCreateSerializer`, and `effective_at` to `JournalEntrySerializer`. Add `PeriodCloseCreateSerializer` and `PeriodCloseSerializer`. Verify with API tests: a backdated create returns 201 with `effective_at`; a naive timestamp returns 400 `invalid`; a future one returns 422 `invalid_effective_at`; a closed period returns 422 `period_closed`; retrieve includes `effective_at`.
- [x] 3.2 Add `PeriodCloseViewSet` (`create` with `Idempotency-Key` and replay, `list` returning `current_cutoff` and `results` newest first) and register `period-closes` in `api/urls.py`. Map `PeriodCloseError` codes, the `period_closed`/`invalid_period_cutoff` `IntegrityError` prefixes, and the new `PostingError` codes in `api/exceptions.py`. Verify with API tests: a close returns 201; a replay returns `Idempotent-Replayed: true` with no second row; a missing key returns 400; a future or non-monotonic cutoff returns 422 `invalid_period_cutoff`; list output is tenant-isolated and ordered.

## 4. Scheduled close

- [x] 4.1 Add the `close_due_periods` Celery task (single-runner `pg_try_advisory_lock`, `for_each_tenant`, records a `SCHEDULED` close only when `scheduled_cutoff` is later than the current cutoff). Add the daily 00:15 UTC entry to `CELERY_BEAT_SCHEDULE`. Verify with tests in `tests/test_tasks.py` or `tests/test_periods.py`, using a frozen `now`: a tenant with a policy is closed; a tenant without a policy is untouched; a second run is a no-op; a run after a multi-month gap catches up in one close.
- [x] 4.2 Add the `manage.py set_period_close_policy --tenant_id <uuid> (--grace_days N | --disable)` command, which validates 0–27 and an existing tenant. Verify with a test that calls `call_command` and checks the stored value, and that it rejects out-of-range values.

## 5. Documentation & integration

- [x] 5.1 Update `README.md`: tick the roadmap item; add the period-close row to the invariants table; add `period_closes` and the new column to Schema; add the new endpoints and `effective_at` to API; add the three error codes to the error contract; add a short "Period close" subsection under Core Design (exclusive cutoff, advisory-lock protocol, corrections via reversal). Verify that the README renders and that every endpoint and error code it mentions exists in code.
- [x] 5.2 Run the full suite (`docker compose run --rm owner pytest --create-db -p no:cacheprovider --ignore-glob='*redteam*'`) and verify that all tests pass: the 106 baseline tests plus the new ones.

## Workflow follow-up

- Archive the change (`/opsx:archive`) after review. This syncs `ledger/period-close`, `ledger/posting`, and `api/http-gateway` into the main specs.
- The user pushes the branch. Do not push automatically.
