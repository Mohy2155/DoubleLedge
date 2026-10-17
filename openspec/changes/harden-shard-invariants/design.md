# Design

## Context

See proposal.md (Why). Relevant current state:

- **Shard creation:** `create_sharded_account` (`ledger/services/sharding.py`) inserts the master with its `shard_count` and inserts shards with `allow_negative=True`, `shard_count=0`, and ids `uuid5(master_id, "shard_<i>")`. Nothing updates `shard_count` or `parent_account_id` afterwards.
- **Existing account constraints:**
  - `accounts_check` (balance ≥ 0 unless `allow_negative`)
  - `accounts_shard_count_check`
  - `accounts_parent_shard_check` (shards have `shard_count = 0`)
  - `shard_allow_negative_exclusion` (a sharded master must allow negative)
- **No triggers on `accounts`.** Trigger errors elsewhere follow the 0013 convention: the message starts with an error code, and `ERRCODE '23514'` makes Django raise `IntegrityError`.
- **Red-team files:**
  - the eight `test_redteam_*.py` files on commit `0510991`, 37 tests in all
  - re-run per file on a fresh database with `max_connections=500`: 26 pass, 2 are open gaps, 4 are fixed findings, 3 are built on a forbidden configuration, and 2 fail in fixture setup
- **Test cleanup between tests:** `transaction=True` tests are cleaned up by Django's flush, which skips unmanaged tables. Tests that need unique rows therefore use unique names.

## Goals / Non-Goals

**Goals:**
- The database enforces both shard rules, whatever role or code path writes.
- The red-team coverage that still means something runs in the default suite on a stock Postgres.

**Non-Goals:**
- Making the rest of an account immutable (`type`, `currency`, `tenant_id`). Worth doing, but it is a separate decision with its own blast radius.
- Changing hold or posting behavior. No service code changes.
- Raising `max_connections`, or any change to the `db` service.

## Decisions

### D1. One trigger for topology, one CHECK for shard strictness

`0015_shard_invariants` adds:
- **`accounts_shard_topology_immutable()`**, run by `BEFORE UPDATE OF shard_count, parent_account_id ON accounts FOR EACH ROW WHEN (OLD.shard_count IS DISTINCT FROM NEW.shard_count OR OLD.parent_account_id IS DISTINCT FROM NEW.parent_account_id)`. It raises `shard_topology_immutable: ...` with `ERRCODE '23514'`.
  - The `WHEN` clause keeps balance and version updates, which are every posting, free of any function call.
  - `UPDATE OF` alone would still fire on a no-op `SET shard_count = shard_count`; the `WHEN` clause filters that out.
- **`CHECK (parent_account_id IS NULL OR allow_negative)`**, named `accounts_shard_allow_negative`. It is validated on apply.

Alternatives:
- **A CHECK can't compare OLD and NEW**, so immutability needs a trigger.
- **Revoking UPDATE on those columns from `ledger_runtime`** is narrower: it doesn't stop the owner role or a migration. The trigger covers every role, like the period-close freeze.
- **Making `parent_account_id` immutable** goes slightly beyond the red-team finding. Re-parenting a shard corrupts routing exactly as a changed count does, and nothing re-parents today.

Teardown: Django's flush uses `TRUNCATE`, which is not an `UPDATE`, so the trigger doesn't interfere.

### D2. Land the tests under descriptive names, in the default suite

Rename `test_redteam_<x>.py` to `test_adversarial_<x>.py` (`holds`, `idempotency`, `lock_inversion`, `sharded_holds`, `snapshots`, `webhooks_ssrf`, `reconciliation`). The two invariant gaps go into a new `test_shard_invariants.py` next to the migration's tests. Remove `--ignore-glob='*redteam*'` from the Makefile.

The alternative was to keep the names and drop the glob. Other commands people run (the OpenSpec task templates, README snippets) copy the glob, and the files would silently disappear again.

### D3. Per-test cleanup rules

- **Concurrency:** at most 20 threads, and so at most 20 connections, per test. Barriers shrink to match. `lock_inversion` keeps its mix of operations, scaled from 120 threads to 20.
- **No DDL in tests.** The three tests that drop `shard_allow_negative_exclusion` to build a sharded master with `allow_negative=false` test a configuration the system forbids.
  - `test_authorize_hold_toctou_prevented` is rewritten in a valid configuration (`allow_negative=True`): sharded accounts enforce hold sufficiency regardless, through the `shard_count > 0` branch in `authorize_hold`.
  - `test_capture_hold_skewed_shards_succeeds` and `test_partial_capture_within_hold_capacity_but_no_single_shard_covers_it` are dropped. In a valid configuration shards may go negative, so they assert nothing.
- **"Currently exploitable" tests:**
  - **Shard gaps:** become tests asserting the database rejects the change (D1).
  - **Capture write-path:** the leak test becomes a positive test that a capture on a sharded account posts to a shard and never to the master. Its fixture funds the account first, so hold sufficiency passes.
  - **TOCTOU overdraft and `released_amount`:** dropped, as long as `tests/test_holds_race.py` and `tests/test_holds_fixes.py` assert the same outcomes. Task 2.1 checks this, and converts the test instead if coverage is missing.
  - **Idempotency race:** asserts the correct outcome:
    - one fresh 201, and four 201 replays flagged as replayed
    - one journal entry
    - `captured_amount` 100
    - available balance −100, since the account started at 0
- **Reconciliation full sweep:** the fixtures create negative balances on strict accounts, which `accounts_check` now rejects. Instead, create drift on `allow_negative` accounts in two tenants, so that `balance` disagrees with their lines. Then assert that one integrity run reports both tenants.
- **Shared helpers:** each file keeps its own `_hash`, tenant fixtures and `_seed_shard_directly`, as the existing suite does. Tenant names get a uuid suffix where rows must stay unique.

## Risks / Trade-offs

- **[Suite runtime grows by about 20–30s]** → Acceptable for race coverage. Thread counts are capped, and no test sleeps longer than its barrier timeouts.
- **[Concurrency tests can be flaky]** → Task 3.2 runs the suite repeatedly before the change is marked done. A test that flakes is fixed or dropped, not retried.
- **[A future feature such as resharding needs to change `shard_count`]** → It would ship its own migration that replaces the trigger, deliberately, with the rebalancing logic.
- **[Existing rows violating the new CHECK make the migration fail]** → The dev DB has none, and the application never creates such rows. The failure would be loud and the migration transactional.

## Migration Plan

`migrate` applies 0015. The reverse drops the trigger, the function and the constraint. No data changes.
