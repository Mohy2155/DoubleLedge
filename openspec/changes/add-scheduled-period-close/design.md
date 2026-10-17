# Design

## Context

See proposal.md for motivation. Observations from the current code that shape the approach:

- `journal_entries.effective_at` exists (`timestamptz NOT NULL DEFAULT now()`), but `post_entry` never sets it, and no Python code reads it. Point-in-time balances and snapshots key on `posted_at` and `sequence`, so allowing a backdated `effective_at` does not disturb them.
- Ledger invariants already live in `RunSQL` migrations as triggers (the deferred balance trigger, and `block_modification()` for immutability). The API pre-validates for friendly errors and the trigger is the backstop. This change follows the same split.
- Postings run at `READ COMMITTED`, lock accounts in primary-key order, and use `lock_timeout = '2s'`. Workers iterate tenants with `for_each_tenant()` as `ledger_runtime`, and the runtime role gets `SELECT, INSERT` plus selective `UPDATE`/`DELETE` grants (0012). New tables need explicit grants.
- Mutating endpoints use `get_or_insert_idempotency_key()` in the same transaction and replay the stored response.

## Goals / Non-Goals

**Goals:**
- The cutoff check runs inside the database for every insert path, and it stays correct when a close commits during a posting.
- Postings that don't backdate add only cheap overhead and no contention between postings of the same tenant.
- Reuse the existing idempotency, RLS, tenant-iteration, and error-mapping machinery. No new dependencies.

**Non-Goals:**
- Reopening periods (an owner-role migration is the deliberate escape hatch).
- Fiscal calendars, non-UTC month boundaries, and per-account or per-currency cutoffs.
- Exposing close policy configuration over HTTP. It is operator-level for now, like sharding.
- Changing `as_of` semantics, statements, or reconciliation.

## Decisions

### D1. Append-only `period_closes` history, not a mutable "lock date" column

```sql
CREATE TABLE period_closes (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  uuid        NOT NULL REFERENCES tenants(id),
    cutoff     timestamptz NOT NULL,
    source     text        NOT NULL CHECK (source IN ('MANUAL', 'SCHEDULED')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_period_closes_tenant_cutoff ON period_closes (tenant_id, cutoff DESC);
```

The current cutoff is `max(cutoff)` for the tenant. History gives an audit trail of who closed what and when, for free, and append-only fits the ledger's ethos: `block_modification()` triggers for `UPDATE`/`DELETE`/`TRUNCATE`, plus a runtime grant of `SELECT, INSERT` only. *Alternative:* a `tenants.closed_before` column. It is simpler to read, but it is mutable, needs an `UPDATE` grant on `tenants`, and loses history. A uuid primary key (as on `holds`) avoids needing a sequence grant.

### D2. Exclusive cutoff (`effective_at < cutoff` is frozen)

Month boundaries are then exact: `cutoff = 2026-11-01T00:00Z` freezes all of October and nothing in November. An inclusive "closed through" timestamp would force `23:59:59.999999` arithmetic.

### D3. Enforcement via `BEFORE INSERT` row triggers on `journal_entries` and `transaction_lines`

One function, `assert_period_open()`. For `journal_entries` it reads `NEW.effective_at`. For `transaction_lines` it looks up the parent entry's `effective_at` by primary key, which closes the "append lines to an old entry" path. It then compares against `max(cutoff)` and raises `check_violation` (`23514`, the same class the balance trigger uses) with message prefix `period_closed:`. Django surfaces this as an `IntegrityError`. `api/exceptions.py` maps the prefix to `422 period_closed` before the generic check-constraint mapping.

A `BEFORE INSERT` trigger on `period_closes` (`assert_period_close_valid()`) enforces `cutoff <= now()` and `cutoff > current max`, with message prefix `invalid_period_cutoff:`.

*Alternative:* a deferred constraint trigger, like the balance check. It is not needed: `effective_at` is known at insert time, and failing early avoids wasted work. *Alternative:* a statement-level trigger with transition tables. It cuts lookups for bulk line inserts, but it is more code for a 2–4-line typical entry.

### D4. Race safety with a per-tenant advisory lock, shared for postings and exclusive for closes

Problem: under `READ COMMITTED`, posting P can read the old cutoff, then close C commits, then P commits a now-frozen entry.

- `assert_period_open()` first runs `pg_advisory_xact_lock_shared(<ns>, hashtext(tenant_id::text))`, then reads `max(cutoff)`.
- `assert_period_close_valid()` first runs `pg_advisory_xact_lock(<ns>, hashtext(tenant_id::text))`, then validates.

Shared locks don't conflict with each other, so postings never contend. A close waits for in-flight postings of that tenant to commit (P serializes *before* C, which is legal), and postings that arrive meanwhile queue behind C. Because trigger functions are `VOLATILE`, each statement takes a fresh snapshot under `READ COMMITTED`. So the `max(cutoff)` read that runs after the lock is granted sees C's committed row, and P is rejected. The two-`int4` advisory key space is disjoint from the single-`bigint` keys used by the reconciliation and rollup tasks, so the locks cannot collide with them. `hashtext` collisions between tenants only cause harmless extra waiting.

*Alternative:* `SELECT ... FOR KEY SHARE` on the tenant row. It is equivalent semantically, but every posting would write a multixact onto the hot `tenants` row. Advisory locks live only in the lock manager. *Alternative:* `SERIALIZABLE`. It is rejected project-wide (retry storms).

Deadlock analysis: postings take the advisory lock *after* their account row locks and never wait on anything a close holds. A close holds only its own idempotency row. So there is no cycle. Waits are bounded by the existing `lock_timeout` and surface as `503 lock_timeout` with `Retry-After`.

### D5. Service-level pre-check for friendly errors; idempotency replay wins

`post_entry` keeps its order: the idempotency lookup comes first, so a replay of a request that succeeded before the close still returns its stored 201. Then it validates `effective_at` (aware, `<= now` → else `invalid_effective_at`) and compares it with `get_cutoff(tenant_id)` → `period_closed`, both before any write. Since `PostingError` aborts the atomic block, the idempotency key is never consumed. The DB trigger remains the authority (D3/D4).

### D6. `effective_at` serialization

API input is a DRF `DateTimeField` subclass that rejects naive timestamps (consistent with `?as_of=`). Responses render `effective_at` as UTC ISO 8601 with a `Z` suffix. `post_entry` adds it to `response_body`, which is also the outbox payload, so webhooks get it with no extra code. The retrieve serializer gains `effective_at`.

### D7. Scheduled close: nullable `tenants.period_close_grace_days` and a daily beat task

- `period_close_grace_days int NULL CHECK (period_close_grace_days BETWEEN 0 AND 27)`. `NULL` means off. The cap of 27 guarantees that "this month isn't eligible yet" only ever steps back exactly one month, even in February.
- `scheduled_cutoff(now, grace_days)`: start of the current UTC month if `now >= month_start + grace_days`, else start of the previous month. The task records a `SCHEDULED` close only when that value is later than the current cutoff. That makes repeated runs no-ops and lets one run catch up after downtime.
- `workers.tasks.close_due_periods` runs daily at 00:15 UTC. It uses the existing `pg_try_advisory_lock` single-runner pattern and `for_each_tenant`. A per-tenant failure (for example a lock timeout) is logged and retried the next day.
- The policy is set by `manage.py set_period_close_policy --tenant_id <uuid> (--grace_days N | --disable)`, which runs as the owner (the runtime role has no `UPDATE` on `tenants`).

*Alternative:* a separate `period_close_policies` table. That would be cleaner if policies grow fields (timezone, frequency), but it is premature now, and the column can move later.

### D8. Close API

`PeriodCloseViewSet` (`create`, `list`) under `tenant_transaction()`. `create` follows the holds pattern: `get_or_insert_idempotency_key`, then `close_period(tenant, cutoff, source='MANUAL')`, then the response is stored on the key row. The service pre-validates (future / not after current) for the friendly `422 invalid_period_cutoff`. A concurrent close that wins the race is still caught by the trigger and mapped to the same code. `list` returns `{"current_cutoff": ..., "results": [...]}` without pagination (about 12 rows a year).

## Risks / Trade-offs

- [A close stalls a tenant's postings while in-flight postings drain] → The window is milliseconds and happens about once a month. Both sides are bounded by `lock_timeout` and turn into retryable 503s.
- [Per-line trigger overhead: one primary-key lookup plus one indexed `max`] → Measured against the benchmark suite. Typical entries have 2–4 lines.
- [App-server clock vs DB clock: `effective_at` defaults to Django's `timezone.now()`, while the close check uses DB `now()`] → Only matters within seconds of a cutoff. Scheduled cutoffs are month boundaries that are days in the past.
- [UTC-only months don't match tenants with local fiscal calendars] → Documented non-goal. Manual closes accept any past cutoff, so such tenants can close on their own boundary through the API.
- [Backdating lets clients write into the open part of the past] → This is intended. The period close is the control that bounds it.

## Migration Plan

1. Migration `0013_period_close` is additive: a new table, a nullable column, new triggers, and grants. There is no backfill, and existing tenants have no cutoff, so behavior is unchanged until someone closes a period.
2. Deploy API and workers. The beat entry is inert until a tenant has a policy.
3. Rollback: the reverse SQL drops the triggers, functions, table, and column. Data written with backdated `effective_at` stays valid.
