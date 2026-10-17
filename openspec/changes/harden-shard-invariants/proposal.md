# Proposal

## Why

The red-team tests on `redteam/threat-model-scaffolding` (written 2026-10-06, never landed) were re-run against today's `main`, each file in isolation on a fresh database. Two sharding gaps are still open, and both are already listed under Known Gaps in the README:

- **`shard_count` can be changed after shards exist.** Routing derives shard ids from the master id and `shard_count`. A changed count routes postings to shards that don't exist, or strands balances in shards that are no longer routed. A changed `parent_account_id` would corrupt the topology the same way.
- **A shard can have `allow_negative = false`.** The rollup and reversals rely on shards going negative. The only check today is on the master (`shard_allow_negative_exclusion`).

The application never makes either change: `create_sharded_account` sets both at insert and nothing updates them. The database does not enforce it, so raw SQL or a future code path can.

The red-team tests themselves are worth keeping, but not as they are:
- **26 pass on `main` and cover real races:** sharded holds, lock inversion, snapshots, idempotency routing, SSRF.
- **They can't land as written:**
  - some open 50–120 connections, beyond Postgres's default of 100
  - one file drops and re-adds constraints (including one not in the schema), which alters the schema for every later test
  - several fixtures create states that today's constraints reject
  - the Makefile skips `*redteam*` files
- **The README's "unconfirmed idempotency race" is a test bug, not a ledger bug.** The test measured the balance change from 100 when the account started at 0, and it counted replays as double successes.

## What Changes

- **Database (migration 0015):**
  - a trigger rejects any change to an account's `shard_count` or `parent_account_id` after insert
  - a CHECK requires `allow_negative = true` on every shard
- **Tests:** land a cleaned set of the red-team tests as regular tests, with descriptive names, in the normal suite:
  - at most 20 concurrent connections per test
  - no DDL inside tests
  - the 3 tests built on a forbidden configuration (a sharded master with `allow_negative = false`) are dropped, and one of them is rewritten in a valid configuration
  - the "currently exploitable" tests become tests of the secure behavior; the two whose findings are already covered on `main` are dropped
  - the idempotency race test asserts the correct outcome
- **Makefile:** remove the `--ignore-glob='*redteam*'` that hid these files.
- **README Known Gaps:** remove the two sharding gaps, `released_amount` (fixed in `419158d`) and the unconfirmed idempotency race (explained above), and drop the line about red-team tests waiting on a branch.
- **Left behind:** the branch's `docker-compose.override.yml` changes `db`'s command, which is the pattern that recreates `db` on an empty volume. The branch's stale `redteam-threat-model` OpenSpec change describes the October state. Neither is carried over, and the branch is deleted after merge.

## Capabilities

### New Capabilities
None.

### Modified Capabilities
- `ledger/sharding`: adds two database-enforced requirements, immutable shard topology and shards that allow negative balances.

## Impact

- **Code:** new migration `ledger/migrations/0015_shard_invariants.py`. No service code changes, since the services already keep both rules.
- **Database:**
  - raw `UPDATE`s of `shard_count` or `parent_account_id` now fail
  - inserting or updating a shard with `allow_negative = false` now fails
  - existing data is valid: the dev DB has no violating rows, and the migration validates the CHECK on apply
- **Tests:** about 30 new tests in the default suite, adding roughly 20–30s to a full run.
- **Docs:** README Known Gaps; Makefile `test` target.
