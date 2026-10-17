# Design

## Context

See proposal.md for motivation. Current-state facts that shape the approach:

- The balance and statement actions run inside `tenant_transaction()`, which opens an atomic block **on the default alias only** and sets `app.tenant_id` there. Their queries go through the ORM on the default alias, plus raw `connection.cursor()` in `BalanceQueryService` and `resolve_member_ids`.
- RLS policies use `nullif(current_setting('app.tenant_id', true), '')::uuid` and fail closed. A replica connection without the setting returns zero rows, not an error. `set_config(..., true)` only lasts until the end of the transaction, so the replica needs its own transaction.
- A physical replica carries the same roles, grants, and RLS policies. `ledger_runtime` can log in to it unchanged.
- Verified against a prototype `postgres:16.15` hot standby:
  - As `ledger_runtime`, `pg_current_wal_lsn()` (on the primary) and `pg_last_wal_replay_lsn()` / `pg_last_xact_replay_timestamp()` (on the replica) are readable.
  - `pg_stat_wal_receiver.status` is **hidden** (NULL) from `ledger_runtime`.
  - RLS on the standby filters exactly like on the primary (1000 of 1000 tenant entries), and writes fail with `read-only transaction`.
- Django test mirrors (`TEST: {MIRROR: 'default'}`) get a *separate* connection to the test DB, so replica-path tests must use `transaction=True`.

## Goals / Non-Goals

**Goals:**
- Correctness first: never serve a replica read that is staler than the consistency level allows, and never let RLS silently empty a result.
- Zero behavior change and zero replica contact when `DB_REPLICA_HOST` is unset.
- Explicit, per-call routing. No global router that could send a `SELECT ... FOR UPDATE` or a read-before-write to the replica.

**Non-Goals:**
- Load balancing across multiple replicas, waiting for the replica to catch up, or exposing lag metrics.
- Production replication setup (slots, failover, a dedicated replication role). The compose override is for development and demos.

## Decisions

### D1. Explicit `using=alias`, not a Django database router

A router's `db_for_read` also routes `select_for_update()` and every incidental read, including reads inside write paths. That is exactly where a stale replica breaks invariants. Instead, a context manager yields an alias, and the two views pass it explicitly to the ORM (`.using(alias)`) and to the two helpers that open raw cursors (`BalanceQueryService.get_account_balance(..., using=)` and `resolve_member_ids(..., using=)`, both defaulting to `'default'`). *Alternative:* a contextvar-driven router. It is less code at call sites, but the scope is implicit, and raw `connection.cursor()` calls would still need changes.

### D2. Freshness check: WAL position first, replay lag second

`choose_read_alias(consistency)`:
1. On the primary (default connection, already open): `SELECT pg_current_wal_lsn()` gives `start_lsn`.
2. On the replica, one query:
   ```sql
   SELECT CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn() ELSE pg_current_wal_lsn() END >= %(start_lsn)s::pg_lsn,
          EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp())
   ```
3. `caught_up` means the replica has everything committed before the request started, so it is served for both levels (read-your-writes). Otherwise `bounded` accepts a lag `<= READ_REPLICA_MAX_LAG_SECONDS`, and a NULL lag counts as stale. `strong` goes to the primary.

Why both checks:
- A replay-timestamp lag alone grows while the primary is idle, which wrongly pushes reads to the primary.
- "receive LSN == replay LSN" alone reports a *disconnected* replica as fresh forever, and the WAL-receiver status that would detect that is not visible to `ledger_runtime`.
- Comparing against the primary's live LSN is privilege-free and correct both when idle and when disconnected. With a disconnected replica, the primary LSN advances, the replay timestamp freezes, and lag grows past the bound.

The `CASE` lets the `replica` alias point at a primary (tests, or misconfiguration) and still count as caught up. *Alternative:* granting `pg_read_all_stats` to `ledger_runtime`. It is rejected because it widens the runtime role for monitoring.

### D3. Replica reads in their own transaction with the tenant set

`tenant_reads(tenant_id, consistency)` yields the alias. For the replica, it wraps the body in `transaction.atomic(using='replica')` and runs `set_config('app.tenant_id', tenant, true)` first. The primary path relies on `tenant_transaction()`, which has already set the tenant. Exceptions (for example a 404) roll back the replica transaction normally. `hot_standby_feedback=on` on the standby keeps primary vacuum from cancelling these short reads.

### D4. Opt-in configuration, and a mirror alias for tests

`settings.DATABASES['replica']` is always defined (copying `default`, with `HOST`/`PORT` from `DB_REPLICA_HOST`/`DB_REPLICA_PORT`, `connect_timeout=2`, `TEST.MIRROR='default'`). `READ_REPLICA_ENABLED = bool(DB_REPLICA_HOST)`. Django connects lazily, so an unused alias costs nothing. Tests flip `READ_REPLICA_ENABLED` and exercise the real code path against the mirror connection.

### D5. Circuit breaker on connection failure

An `OperationalError` while checking the replica closes that connection and sets a process-local `_unavailable_until = monotonic() + 30`. Until then, `choose_read_alias` returns `default` without touching the replica. *Alternative:* retrying the replica on every request. That costs a 2-second connect timeout on every request during an outage.

### D6. Local streaming replica via a compose override

`docker-compose.replica.yml` overrides the primary's `command` to use a mounted `db-replica/pg_hba.conf`. That file is the image defaults plus `host replication all all scram-sha-256`; an `hba_file` override also works on existing volumes, where `db-init` scripts never re-run. It adds `db_replica` (`postgres:16.15`, `user: postgres`), which runs `pg_basebackup -R -X stream` from `db` into an empty volume and then `postgres -c hot_standby_feedback=on`. It sets `DB_REPLICA_HOST=db_replica` on `api`. Basebackup uses the dev superuser credentials. A dedicated replication role is a production concern and out of scope. `make replica` runs `docker compose -f docker-compose.yml -f docker-compose.replica.yml up -d --wait`.

## Risks / Trade-offs

- [Two extra round-trips per routed read] → Both are sub-millisecond catalog function calls. The primary connection is already open from `tenant_transaction()`.
- [Clock skew between replica and primary distorts the time-based lag] → It only affects the `bounded` fallback path, and the WAL-position check is skew-free. Documented.
- [Under heavy write load the replica is rarely "caught up", so `strong` reads stay on the primary] → This is intended. `bounded` (the default) still offloads.
- [The circuit breaker is per process] → Each Gunicorn worker learns independently, which is at most one failed connect per worker per 30 seconds.
- [The superuser is used for basebackup in the dev override] → Scoped to the local override file and called out in the README.

## Migration Plan

No schema migration. Deploy the code, then set `DB_REPLICA_HOST` (and optionally `READ_REPLICA_MAX_LAG_SECONDS`) to enable it. Roll back by unsetting `DB_REPLICA_HOST`.
