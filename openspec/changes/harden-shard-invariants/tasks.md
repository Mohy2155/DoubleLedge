# Tasks

## 1. Database shard invariants

- [ ] 1.1 Write `tests/test_shard_invariants.py` (`transaction=True` where needed) before the migration exists, and confirm the rejection cases fail on the current schema. Cases:
  - updating a sharded master's `shard_count` (raw SQL, as the owner role) is rejected with `shard_topology_immutable`
  - updating a shard's `parent_account_id` is rejected
  - a no-op `SET shard_count = shard_count` and a normal posting to the sharded account still succeed
  - inserting a shard with `allow_negative = false` is rejected by `accounts_shard_allow_negative`
  - updating an existing shard to `allow_negative = false` is rejected
- [ ] 1.2 Add `ledger/migrations/0015_shard_invariants.py` (design D1): the immutability trigger function and trigger, the CHECK, and a reverse that drops them. Verify:
  - `migrate` applies it on the dev DB
  - a reverse migration and reapply both work
  - `makemigrations --check` is clean
  - the 1.1 tests pass
  - `tests/test_sharding*.py` and `tests/test_holds*.py` still pass

## 2. Land the red-team tests

- [ ] 2.1 Check that `tests/test_holds_race.py` and `tests/test_holds_fixes.py` already assert the TOCTOU-overdraft rejection and `released_amount` in void and expire events. If either is missing, convert the matching red-team test into a test of the secure behavior instead of dropping it. Record which applied in the commit message.
- [ ] 2.2 Bring over the red-team files from commit `0510991` as `tests/test_adversarial_*.py` (design D2), applying design D3:
  - cap concurrency at 20 threads
  - no DDL
  - drop the two forbidden-configuration tests, and rewrite `test_authorize_hold_toctou_prevented` with `allow_negative=True`
  - turn the capture write-path test into a positive "posts to a shard" test
  - make the idempotency race test assert the correct outcome
  - rewrite the reconciliation full-sweep fixtures

  Verify that each new file passes on its own and that `grep -rn "ALTER TABLE\|max_workers=\(2[1-9]\|[3-9][0-9]\|1[0-9][0-9]\)" tests/test_adversarial_*.py` finds nothing.
- [ ] 2.3 Remove `--ignore-glob='*redteam*'` from the Makefile `test` target. Verify that `make test` (or its command) collects the `test_adversarial_*` and `test_shard_invariants` files.
- [ ] 2.4 Update README Known Gaps:
  - remove "Sharding Constraints"
  - remove `released_amount` from "Holds Invariant", keeping the `held_amount` item
  - replace the unconfirmed idempotency race with nothing, since it's explained (proposal Why)
  - remove the "red-team tests stay on a separate branch" line

  Add the two shard rules to the Core Design sharding section if it describes shard constraints. Verify that every name the edited text mentions exists in code.

## 3. Integration

- [ ] 3.1 Run the full suite (`docker compose run --rm --no-deps owner pytest --create-db -p no:cacheprovider`, now without the ignore glob). Verify that everything passes: the 235 baseline tests plus the new ones.
- [ ] 3.2 Run the full suite 10 more times. Verify there are no failures and no `too many clients` errors. Fix or drop any flaky adversarial test and record why.
- [ ] 3.3 Run `make stress` and `make integrity` on the dev stack (`--no-deps`). Verify that the demo posts all transfers and integrity reports 0 discrepancies.

## Workflow follow-up

- Commit on `feat/harden-shard-invariants` (no attribution), push, and open a PR for the user to review and merge.
- After the merge, archive the change and delete the local `redteam/threat-model-scaffolding` branch. Its compose override and stale OpenSpec change are intentionally not carried over.
