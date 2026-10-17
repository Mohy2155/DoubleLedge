# Tasks

## 1. Per-currency posting check

- [ ] 1.1 Move `_direction_to_decrease` / `_direction_to_increase` from `ledger/services/holds.py` to `ledger/services/posting.py` and import them in `holds.py`. Add the per-currency pre-check to `post_entry` after accounts are locked (design D7). Verify with new cases in `tests/test_api_endpoints.py`:
  - a USD debit with a EUR credit of the same amount returns 422 `unbalanced_entry`, and no entry, line or idempotency row is stored
  - an entry balanced within each of two currencies posts 201
  - the existing suite still passes, including the holds tests

## 2. Schema

- [ ] 2.1 Add migration `0014_fx_conversions` (design D6) and the `FxConversion` model (`managed = False`). Verify `migrate` and `makemigrations --check` are clean, and verify with `tests/test_fx_conversions_db.py`:
  - an insert whose `to_amount` breaks the rounding `CHECK` is rejected
  - equal currencies are rejected
  - `UPDATE`, `DELETE` and `TRUNCATE` are rejected
  - under `ledger_runtime`, another tenant's rows are invisible
  - a row referencing another tenant's account or entry is rejected by the composite foreign keys

## 3. Conversion service

- [ ] 3.1 Create `ledger/services/fx.py` (design D2–D5) with these pieces, and verify the trading-account and validation parts in `tests/test_fx_conversions.py`:
  - `FxError`
  - `fx_trading_account_id`
  - sorted, `ON CONFLICT DO NOTHING` trading-account provisioning
  - rate and `rate_as_of` validation
  - `to_amount` computed with `ROUND_HALF_UP` in a `prec=60` context

  The tests cover:
  - deterministic ids, with trading accounts created once and then reused
  - `1.0000 × 0.12345 = 0.1235`
  - an amount that rounds to zero gives `invalid_amount`
  - a product beyond `numeric(19,4)` gives `invalid_amount`
  - every `invalid_rate` case: `0`, negative, a JSON number, 13 decimal places
  - a future `rate_as_of` gives `invalid_rate_as_of`
  - `same_currency`
  - a trading account as source or destination gives `invalid_account`
- [ ] 3.2 Implement `convert()`: idempotency, then `post_entry` with the derived key, then the stamp row, then the stored response (design D3). Extend `tests/test_fx_conversions.py` to verify:
  - USD asset → EUR asset and USD liability → EUR liability post four lines that match the stamp, and update all four balances as the spec describes
  - two concurrent first conversions (threads, `transaction=True`) both succeed, and exactly one trading account per currency exists
  - insufficient funds writes nothing: no entry, no stamp, no trading accounts
  - a closed period gives `period_closed`
  - a replay returns the stored response without a second entry, and reusing a key with a different body gives `idempotency_key_reuse`
  - an `entry.posted` outbox event is emitted
  - a sharded source account routes to a shard and the response reports the master id
  - reversing the entry through the reverse endpoint restores all four balances and leaves the stamp intact

## 4. API and documentation

- [ ] 4.1 Add the serializers, `FxConversionViewSet` (create, list, retrieve), the `fx-conversions` route, and the `FxError` mapping in `api/exceptions.py` (design D8). Verify with `tests/test_fx_conversions_api.py`:
  - 201 with the full response shape, including currency on each line
  - `Idempotent-Replayed: true` on replay
  - 400 `missing_idempotency_key`
  - the list comes back newest first
  - retrieve returns the stamp
  - 404 for another tenant's conversion or account, under `ledger_runtime`
  - each error code from the gateway spec, with its status
  - `/api/docs/` still renders
- [ ] 4.2 Update `README.md`:
  - add a Core Design section "8. Multi-currency conversions" covering trading accounts, the four-line example, half-up rounding, the stamp, and the per-currency pre-check with its `unbalanced_entry` code change
  - document the `fx-conversions` endpoints in the API section
  - tick the roadmap item

  Verify that every name and code the README mentions exists in the code.

## 5. Demo

- [ ] 5.1 Make `seed_demo` create an empty `Cash EUR` account (`get_or_create`), and update `tests/test_demo_bootstrap.py` to expect the five accounts. Verify that test passes, and that the USD balances and the seeded hold match the README walkthrough numbers.
- [ ] 5.2 Update `api/templates/demo/index.html` (design D9):
  - currency column and amount labels taken from the selected account
  - transfers disabled across currencies, with a hint
  - the hold body sends the account's currency
  - a "Convert currency" card and a "Recent conversions" table
  - guide step 5

  Verify in a browser against a fresh `demo_bootstrap` tenant:
  - the original four guide steps produce the same numbers as before
  - a USD↔EUR transfer is disabled with the hint
  - a hold on `Cash EUR` succeeds
  - converting 100 Cash → Cash EUR at 0.92 shows `100.0000 USD → 92.0000 EUR @ 0.92`, the `FX Trading USD/EUR` rows, and the new conversion at the top of the table
  - the page has no console errors
- [ ] 5.3 Add a currency-conversion step to the README demo walkthrough, and refresh `docs/screenshots/01-overview.png` if the accounts table in it is out of date. Verify that the walkthrough steps match what the browser run in 5.2 showed.

## 6. Integration

- [ ] 6.1 Run the full suite (`docker compose run --rm owner pytest --create-db -p no:cacheprovider --ignore-glob='*redteam*'`) and verify that everything passes (169 baseline plus the new tests).
- [ ] 6.2 Repeat the reviewer flow on a fresh clone of the branch, in a separate Compose project: the Quickstart commands, the browser walkthrough including a conversion, `make stress` and `make integrity` (reconciliation passes with trading accounts present), and the replica stack serving a `Cash EUR` statement with `Read-Source: replica`. Verify that each step succeeds, then tear the clone down with the `make down` command.

## Workflow follow-up

- Commit on `feat/fx-conversions`, push, and open a PR for the user to review and merge.
- After merge, archive the change (`/opsx:archive add-fx-conversions`).
