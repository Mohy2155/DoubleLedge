# Proposal

## Why

Accounting periods have to be closeable: once a month is reported, nothing may be booked into it. DoubleLedge already records `effective_at` (business time), but today it is always `now()`. Clients cannot backdate, and nothing stops a raw SQL insert, an admin shell, or a future code path from writing into a period that finance has already reported on. "Scheduled period close" is the next open roadmap item that fits the project's thesis directly: a correctness rule that the **database** enforces, not the application.

## What Changes

- **New `period_closes` table** (append-only, RLS-isolated). Each row moves a tenant's **cutoff** forward. Any journal entry with `effective_at < cutoff` is frozen.
- **Database-enforced freeze.** `BEFORE INSERT` triggers on `journal_entries` and `transaction_lines` reject writes whose `effective_at` falls before the tenant's current cutoff. The rule holds for raw SQL and the runtime role, not only the API.
- **Race-safe close.** A close waits for in-flight postings of that tenant and blocks new ones for the instant it commits. A posting can therefore never "slip into" a period that closed during the posting.
- **Monotonic, past-only cutoffs.** A close must move the cutoff strictly forward and may not be in the future. Reopening a closed period is not possible through the runtime role (no `UPDATE`/`DELETE` grants, and immutability triggers).
- **Backdated postings.** `POST /api/v1/journal-entries/` accepts an optional, timezone-aware `effective_at` that must not be in the future. When omitted, behavior is unchanged (`now()`). Responses and webhook payloads now include `effective_at`.
- **Close API.** `POST /api/v1/period-closes/` (idempotent, `Idempotency-Key` required) and `GET /api/v1/period-closes/` (history plus the current cutoff).
- **Scheduled close.** A per-tenant policy (`tenants.period_close_grace_days`, `NULL` = off) and a daily Celery beat task. Once a month boundary plus the grace days has passed, the task closes the previous UTC calendar month. It is idempotent and catches up after downtime. An operator sets the policy with `manage.py set_period_close_policy`.
- **New error codes.** `422 period_closed`, `422 invalid_effective_at`, `422 invalid_period_cutoff`.
- Corrections to a closed period work exactly as they do today: a **reversing entry** dated in the open period. Reversal, hold capture, and shard rollups always post at `now()`, so they are unaffected.

## Capabilities

### New Capabilities
- `ledger/period-close`: tenant period cutoffs, their database-level enforcement on ledger writes, race safety against concurrent postings, and the scheduled close policy.

### Modified Capabilities
- `ledger/posting`: postings accept an optional client-supplied `effective_at` (business time) with validation, and are rejected when that time falls in a closed period.
- `api/http-gateway`: new period-close endpoints, `effective_at` on journal-entry create and responses, and the new error codes.

## Impact

- **Database:** migration `0013_period_close` adds the `period_closes` table, the trigger functions, the `tenants.period_close_grace_days` column, and grants for `ledger_runtime` (`SELECT, INSERT` only on `period_closes`).
- **Code:** `ledger/services/periods.py` (new), `ledger/services/posting.py`, `ledger/models.py`, `api/serializers.py`, `api/views.py`, `api/urls.py`, `api/exceptions.py`, `workers/tasks.py`, `config/settings.py` (beat entry), and a new management command.
- **Performance:** each journal entry and line insert does one indexed `max(cutoff)` lookup and takes a shared advisory lock, which does not contend with other postings. A close briefly serializes against that tenant's in-flight postings, once per period.
- **Compatibility:** additive. Existing clients that omit `effective_at` see no change, except an extra `effective_at` field in responses and webhook payloads.
- **Out of scope:** reopening periods, non-UTC or fiscal-calendar periods, per-account cutoffs, and changing point-in-time balances (`as_of` stays system time / `posted_at`).
