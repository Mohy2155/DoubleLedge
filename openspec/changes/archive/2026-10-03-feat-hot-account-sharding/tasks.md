# Tasks

## 0. Resolve the open question first

- [ ] 0.1 Check `api/views.py` / `api/urls.py` for an existing account-creation endpoint (`AccountViewSet.create` or equivalent). If one exists, Phase 2 extends it instead of adding a management command; update this tasks file accordingly before proceeding past Phase 2.

## 1. Schema

- [x] 1.1 Create `ledger/migrations/0009_sharding.py` (RunSQL) adding `shard_count`, `parent_account_id`, the three CHECK constraints, and the **composite, tenant-scoped** self-referencing FK (`FOREIGN KEY (tenant_id, parent_account_id) REFERENCES accounts (tenant_id, id)`) per design.md Decision 1. No new table, so no new `GRANT` statements. Verify: `python manage.py migrate` succeeds; inserting a shard whose `parent_account_id` belongs to a different tenant fails; a row cannot have both `shard_count > 0` and a non-null `parent_account_id`.
- [x] 1.2 Update the `Account` model with `shard_count` and `parent_account_id` fields (`managed = False`, matching every other model). Verify Django can query the new columns and the model still round-trips through `makemigrations --dry-run` with no unexpected changes.

## 2. Account Provisioning

- [x] 2.1 Implement `ledger/services/sharding.create_sharded_account(...)` per design.md Decision 2 (single atomic transaction: master with `shard_count=0` → bulk-create shards → update master's `shard_count`). Verify: creating with `shard_count=5` yields exactly 6 rows, all same tenant/currency/type; a simulated failure mid-creation (e.g. forced exception after shard insert) leaves zero rows behind, not a partial master.
- [x] 2.2 Add a thin `manage.py create_sharded_account` wrapper (or extend the existing endpoint per task 0.1). Verify: `shard_count` outside `[0, 64]` is rejected with a clear error before touching the database.

## 3. Posting Engine Routing

- [x] 3.1 Update `post_entry` to perform the dirty read and rewrite fresh lines targeting a sharded master to a resolved shard id, then lock on resolved ids exactly as today (design.md Decision 3, steps 1–4). Verify: a unit test posting to a sharded account creates a line pointing at a shard id, and the account-ids-to-lock set is still sorted/deduped as before.
- [x] 3.2 Confirm (and add a test proving) that reversal entries copy original lines' resolved `account_id` verbatim and are never re-routed. Verify: reversing an entry originally posted to shard `S_i` produces a reversing line against `S_i`, never a different, freshly-chosen shard — run this with `shard_count` large enough that a different random choice would be overwhelmingly likely if routing were (incorrectly) applied.
- [x] 3.3 Add `bypass_sharding` parameter to `post_entry`, used only by the rollup task (Phase 6), that skips step 2 of routing for the specified line(s). Verify: a `bypass_sharding=True` call targeting the master id does not get rewritten to a shard.

## 4. Aggregate Reads: Balance & Holds

- [x] 4.1 Implement `ledger/services/sharding.resolve_member_ids` and `get_aggregate_balance` per design.md Decision 4. Verify: `posted_balance` for a sharded master with no rollups yet equals the sum of its shards' `balance` (not zero); `available_balance` correctly subtracts pending holds that reference the master id.
- [x] 4.2 Update `GET /api/v1/accounts/{id}/` to source `posted_balance` / `pending_holds` / `available_balance` from `get_aggregate_balance`. Verify against a non-sharded account too (must be a no-op there — `resolve_member_ids` returns a single-element list).
- [x] 4.3 Update the existing `GET /api/v1/accounts/{id}/balance/?as_of=` endpoint to aggregate `BalanceQueryService.compute(member_id, as_of)` across all member ids. Verify a historical `as_of` balance for a sharded account matches a brute-force recomputation from raw lines across all shards.
- [x] 4.4 Check `ledger/services/holds.py` for any available-balance validation at hold creation/capture time; if present, point it at `get_aggregate_balance` instead of `Account.balance`. Verify: creating a hold against a sharded account whose master row has zero balance (pre-rollup) but whose shards collectively cover the amount succeeds.

## 5. Logical-Id Mapping

- [x] 5.1 Implement `ledger/services/sharding.to_logical_account_id` with batched lookup, per design.md Decision 5. Verify a shard id and a non-shard id both resolve correctly, batched over a list.
- [x] 5.2 Apply it to `JournalEntrySerializer` / `TransactionLineSerializer` read paths, to the outbox payload construction in `post_entry` (so webhook deliveries carry logical ids), and to statement line serialization. Verify end-to-end: post to a sharded account, fetch the entry via the API, inspect the resulting webhook payload (mocked receiver) — no shard id appears anywhere in either.
- [x] 5.3 Make `GET /api/v1/accounts/{id}/` return `404` when `{id}` resolves to a shard (`parent_account_id IS NOT NULL`). Verify directly and verify cross-tenant shard lookups still 404 via RLS as before.

## 6. Statement Aggregation

- [x] 6.1 Confirm the exact ordering/cursor fields the current single-account statement endpoint uses before changing anything (design.md Decision 6 — do not invent a new scheme). Extend the query to union `transaction_lines` across all member ids, preserving that same ordering and cursor contract.
- [x] 6.2 Implement the aggregate running balance via a window function over the unioned rows, seeded the same way the existing endpoint seeds its first page. Verify: total of a full paginated walk through a sharded account's statement equals `get_aggregate_balance`'s current `posted_balance`; pagination remains stable when a rollup runs between two page fetches.
- [x] 6.3 Apply Decision 5's logical-id mapping to every statement line.

## 7. Rollup Sweep Task

- [x] 7.1 Implement `rollup_shards` in `workers/tasks.py` using `for_each_tenant` (design.md Decision 7). For each tenant, sweep shards with non-zero balance and call `post_entry(..., bypass_sharding=True)` with a deterministic `reference_id` derived from shard state (not a timestamp). Verify: running the task twice in a row (second run immediately after the first) moves money exactly once per shard, with the second run's duplicate `reference_id` correctly hitting the existing idempotency/replay path and making no further ledger change.
- [x] 7.2 Add `pg_try_advisory_lock`-based overlap guard per tenant, consistent with other periodic tenant-scoped tasks. Add to Celery beat schedule. Verify: two concurrently-triggered runs for the same tenant result in only one doing the actual sweep.

## 8. Reconciliation (verify, don't modify)

- [x] 8.1 Confirm `ledger/services/reconciliation.py` iterates all `accounts` rows unconditionally (no `parent_account_id IS NULL` filter anywhere). Verify `manage.py check_ledger_integrity` passes unmodified on a ledger containing sharded accounts, both before and after a rollup.
- [x] 8.2 (Optional) Add an informational aggregate cross-check per sharded master to `ReconciliationReport.discrepancies` for operator visibility. Not required for correctness per design.md Decision 8.

## 9. Chaos & Concurrency Validation

- [x] 9.1 `tests/test_sharding_stress.py`: 50+ threads concurrently posting to the same sharded master; verify materially fewer lock timeouts than an equivalent non-sharded account under the same load, and `Σ(master.balance, shard balances)` still matches `Σ` of all lines after the run.
- [x] 9.2 Thread a rollup sweep concurrently with live postings to the same shards; verify no lost updates and no double-moved money (reuses 7.1's idempotency guarantee under real concurrency, not just sequential re-invocation).
- [x] 9.3 Run the full suite `pytest tests/ -v`. Verify no regressions in holds, idempotency, webhooks, snapshots, or core ledger rules.
