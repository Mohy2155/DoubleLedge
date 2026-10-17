# Proposal

## Why

Statements and point-in-time balances are the ledger's heaviest reads. They scan or aggregate history, and today they compete on the primary with postings that hold row locks. Streaming replicas are the standard way to offload them. But a ledger can't simply point reads at a replica: RLS fails *closed*, so a replica connection without a tenant set returns silently empty results, and replication lag makes "the balance you just changed" disappear. This change adds replica routing that keeps the project's correctness bar: a read is served from the replica only when that is provably fresh enough, and from the primary otherwise.

## What Changes

- **Optional `replica` database.** It is enabled by `DB_REPLICA_HOST`. Without it, behavior is byte-for-byte unchanged and the replica is never contacted.
- **Freshness-checked routing** for `GET /accounts/{id}/statement/` and `GET /accounts/{id}/balance/` (current and `as_of`):
  - The replica serves the read if it has replayed **everything the primary had written when the request started** (WAL position comparison). That gives read-your-writes.
  - Otherwise, under the default `consistency=bounded`, the replica serves the read only if its replay lag is within `READ_REPLICA_MAX_LAG_SECONDS` (default 5).
  - Otherwise the read goes to the primary.
- **`?consistency=strong`** on both endpoints. It never accepts a replica that hasn't caught up to the request's start position. Values other than `strong` or `bounded` return `400`.
- **Tenant isolation on the replica.** Replica reads run in a replica transaction with `app.tenant_id` set, so RLS behaves exactly as on the primary.
- **Graceful degradation.** If the replica is unreachable, the read falls back to the primary, and the replica is skipped for 30 seconds instead of paying a connect timeout on every request.
- **`Read-Source: replica|primary`** response header on routed endpoints, for observability and tests.
- **Local streaming replica.** `docker-compose.replica.yml` adds a hot standby (`pg_basebackup`, `hot_standby_feedback`) and points the API at it. A `make replica` target brings it up. It works on existing data volumes.

## Capabilities

### New Capabilities
- `api/read-replicas`: which reads may be served by a replica, the freshness guarantees (`bounded` and `strong`), tenant isolation on replica connections, fallback behavior, and the `Read-Source` header.

### Modified Capabilities
- `environment/orchestration`: an opt-in local streaming replica for development and demos.

## Impact

- **Code:**
  - `config/settings.py`: `replica` alias (a test mirror of `default`) and two settings.
  - `ledger/services/replica.py` (new): alias choice and tenant-scoped reads.
  - `ledger/services/snapshots.py` and `ledger/services/sharding.py`: accept a `using` alias.
  - `api/views.py`: the balance and statement actions.
- **Infra:** `docker-compose.replica.yml`, `db-replica/bootstrap.sh`, and a `Makefile` target. The base `docker-compose.yml` and the primary container are unchanged.
- **Performance:** each routed read adds one `pg_current_wal_lsn()` call on the primary and one status query on the replica, both sub-millisecond. The history scans move off the primary whenever the replica is fresh.
- **Compatibility:** additive. Responses gain one header, and `consistency` is a new optional query parameter.
- **Out of scope:** routing other endpoints, multiple replicas or load balancing, waiting for a replica to catch up, and production replication management (slots, failover, a dedicated replication role).
