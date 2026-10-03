# Proposal

## Why

High-volume accounts (such as a platform's central fee or clearing account) act as a bottleneck in DoubleLedge because every transaction touching them must acquire the same PostgreSQL row lock (`SELECT FOR UPDATE`). This serializes write throughput, limiting TPS to the reciprocal of the transaction duration. Hot-account sharding solves this by distributing write volume across *N* sub-accounts (shards), bypassing the single-row lock limit while preserving overall balance accuracy, auditability, and atomicity.

Shards are an **internal storage detail**. Every tenant-facing surface (account reads, journal entry reads, statements, webhook payloads) must continue to show the single logical account the tenant created — the previous draft only aggregated balance reads and missed that line-level `account_id` is also tenant-visible in several other places.

## What Changes

- Extend the existing `accounts` table with `shard_count` and `parent_account_id` (self-referencing, composite, tenant-scoped). **No new table is introduced**, so no new `GRANT` statements are required — the existing `accounts` grants already cover the new columns.
- Add CHECK constraints preventing negative/unbounded `shard_count` and preventing a shard from itself being sharded (no nesting).
- Add an internal account-provisioning path (management command, not a new public endpoint — see Decision 2 in design.md and the open question below) that atomically creates one master row and *N* shard rows with deterministic `uuid5` shard ids.
- Modify the posting engine so a line whose `account_id` is a sharded master gets rewritten to a randomly chosen shard **before** lock acquisition, preserving the existing deterministic sort-then-lock order on the resolved ids. Reversal entries copy the original line's already-resolved `account_id` and are **not** re-routed, so a reversal always nets out against the exact shard the original line touched.
- Add an aggregate-balance layer (`ledger/services/sharding.py`) used everywhere a tenant-facing balance or available-balance figure is produced: `GET /accounts/{id}/`, the existing historical `GET /accounts/{id}/balance/?as_of=` (built on `BalanceQueryService` from the snapshots change), and hold validation, so `posted_balance`, `pending_holds`, and `available_balance` are always correct for a sharded account regardless of how recently the rollup sweep last ran.
- Add a **logical-id mapping** layer used everywhere a line's `account_id` is rendered externally (journal entry responses, statement responses, outbox/webhook payloads): a shard id is always translated back to its master id before leaving the system. The physical shard id is never tenant-visible.
- Make `GET /accounts/{id}/` 404 when `{id}` is itself a shard id, since shards are not independently addressable resources.
- Add a periodic Celery rollup task that sweeps shard balances back into the master account via the existing posting engine (with a routing-bypass flag), using a deterministic `reference_id` so the sweep is itself idempotent and safe to retry or run concurrently.
- Extend the account statement endpoint to merge master + shard lines into one chronologically ordered, correctly keyset-paginated stream with an aggregate running balance (not a per-shard running balance).

## Capabilities

### New Capabilities
- `ledger/sharding`: Manages hot-account shards, routes write traffic across shards, aggregates reads, maps physical shard ids back to logical account ids, and periodically rolls up shard balances to the parent account.

### Modified Capabilities
- `api/http-gateway`: Aggregated account retrieval, aggregated historical balance, merged+paginated statement, logical-id mapping on line/entry responses, and shard ids rejected as a direct account lookup.
- `ledger/posting`: Transparent routing of fresh lines to shards; reversal lines explicitly exempt from routing; webhook/outbox payload construction uses logical ids.
- `ledger/holds` *(if this capability exists as written — confirm against `ledger/services/holds.py`)*: Available-balance computation must use the aggregate helper, not `Account.balance` read directly off whichever id was referenced.

### Unmodified (confirmed, not merely assumed)
- `ledger/reconciliation`: Each shard and the master are themselves ordinary rows in `accounts` with their own lines; the existing per-account invariant (`accounts.balance` == recomputed from that account's own lines) already holds for every shard and for the master with zero special-casing, **provided reconciliation iterates all account rows unconditionally** (verify this; do not filter out rows with `parent_account_id IS NOT NULL`). An optional aggregate cross-check (`SUM(master + shards)` reported alongside the per-row results) can be added for operator confidence, but is not required for correctness.
- `ledger/snapshots`: `BalanceQueryService` operates per physical account id and is correct for a shard or a master without modification. The aggregate layer calls it once per member id and sums the results.
- `ledger/idempotency`: Unaffected; routing happens after the idempotency-key transaction boundary is already established.

## Open Question (must resolve before Phase 2)

No `POST /api/v1/accounts/` endpoint exists in the documented API surface (README's endpoint table has no account-creation route). The previous draft assumed a pre-existing `AccountViewSet.create`. Default assumption used below: account creation (sharded or not) stays an **internal/admin operation** (management command), not a tenant-facing endpoint — turning `shard_count` into a tenant-controlled, self-service knob is a separate product decision with its own abuse-control questions (a client requesting `shard_count=10000`). If a `POST /api/v1/accounts/` already exists in `api/views.py`, this assumption is wrong and Phase 2 should extend it instead of adding a management command.

## Non-Goals

- Dynamic resharding (changing `shard_count` after creation).
- Recursive/nested sharding (a shard cannot itself have `shard_count > 0`; enforced by a CHECK constraint).
- A tenant-facing, self-service account/shard-creation endpoint (see Open Question).
- External dependencies for routing or coordination (no Redis; Postgres primitives only).
