# Tasks

## 1. Configuration & routing service

- [x] 1.1 Add the `replica` alias, `READ_REPLICA_ENABLED`, and `READ_REPLICA_MAX_LAG_SECONDS` to `config/settings.py` (design D4). Verify that the existing suite still passes, that `makemigrations --check` is clean, and that no test touches the replica alias unless it declares it.
- [x] 1.2 Create `ledger/services/replica.py` with `choose_read_alias(consistency)` (D2, D5) and `tenant_reads(tenant_id, consistency)` (D3). Verify with unit tests in `tests/test_read_replica.py`, monkeypatching the status query, for:
  - disabled → primary, with no replica query issued
  - caught up → replica, for both levels
  - lag within the bound → replica for `bounded`, primary for `strong`
  - lag over the bound, or NULL lag → primary
  - `OperationalError` → primary, plus a 30-second back-off with no further replica queries
  - against the real mirror alias, the status query reports caught up

## 2. Endpoint integration

- [x] 2.1 Thread `using` through `BalanceQueryService.get_account_balance` and `resolve_member_ids` (default `'default'`). Verify that the snapshot and sharding tests still pass.
- [x] 2.2 Route the `balance` and `statement` actions through `tenant_reads`, validate `consistency` (`bounded`/`strong`, otherwise 400 `invalid`), and set the `Read-Source` header. Verify with API tests (`transaction=True`, `databases=['default', 'replica']`):
  - with the replica enabled, statement and balance (current and `as_of`) return `Read-Source: replica` and the same body as with the replica disabled
  - under the `ledger_runtime` role on both connections, the replica-served statement contains the tenant's lines, and another tenant's account returns 404
  - a lagging replica with `strong` returns `Read-Source: primary`
  - an unknown `consistency` returns 400
  - with the replica disabled, the header is `primary` and the replica alias is never used
  - `GET /accounts/{id}/` is not routed

## 3. Local replica environment

- [x] 3.1 Add `db-replica/bootstrap.sh`, `docker-compose.replica.yml` (D6, which never overrides `db`), and a `make replica` target. Verify end to end against the running stack:
  - the replica reports `pg_is_in_recovery() = true`
  - posting an entry through the API and then fetching the statement returns `Read-Source: replica` with the new entry
  - stopping `db_replica` makes the next statement return `Read-Source: primary` with HTTP 200
  - `docker compose up` without the override starts no replica
- [x] 3.2 Re-clone a replica left over from a previous primary, and make `make down` remove the replica too (D6). Verify in a throwaway Compose project:
  - after a base-only `down -v` and a restart, the old replica (replay LSN ahead of the new primary) serves 404 for new accounts, which reproduces the bug
  - with the fix, the bootstrap logs the identifier mismatch and re-clones, and the same statement returns 200 with `Read-Source: replica`
  - a plain replica restart does not re-clone, and a new entry is then served from the replica
  - `make down` removes the replica container and volume, and is a no-op on an empty project

## 4. Documentation & integration

- [x] 4.1 Update `README.md`:
  - tick the roadmap item
  - add a "Read replicas" subsection under Core Design (freshness rule, `consistency`, RLS on replica, fallback)
  - add the `consistency` parameter and the `Read-Source` header to the API section
  - add `make replica` to Quickstart, and `docker-compose.replica.yml` and `db-replica/` to Repository Layout
  - add the env vars to `.env.example` as commented examples

  Verify that every name the README mentions exists.
- [x] 4.2 Run the full suite (`docker compose run --rm owner pytest --create-db -p no:cacheprovider --ignore-glob='*redteam*'`) and verify that all tests pass (154 baseline plus the new ones).

## Workflow follow-up

- Archive the change after review (`/opsx:archive add-read-replica-routing`).
- The user pushes the branch. Do not push automatically.
