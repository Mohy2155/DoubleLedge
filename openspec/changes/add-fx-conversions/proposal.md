# Proposal

## Why

Every account already has a currency, and the database rejects any entry that doesn't balance within each currency. But the ledger has no way to move value between currencies. Today a USD→EUR conversion can't be posted at all. Trying it with `POST /journal-entries/` fails at commit with a raw `integrity_error`, because the posting service only checks that debits equal credits overall. The README roadmap lists a "Multi-currency FX helper: rate-stamped conversion entries" as the next open item. This change adds conversions that stay balanced in every currency and records the rate each one used, without weakening any existing invariant.

## What Changes

- **`POST /api/v1/fx-conversions/`** (requires `Idempotency-Key`) converts `from_amount` out of a source account into a destination account in another currency, at a client-supplied `rate` (with optional `rate_source` and `rate_as_of`).
  - The server computes `to_amount = from_amount × rate`, rounded half-up to 4 decimal places.
  - It posts **one journal entry with four lines**, using a per-tenant **FX trading account** for each currency (the currency-trading-account method), so each currency balances on its own.
  - The response is the conversion with its rate stamp and the entry's lines.
- **FX trading accounts.** One `EQUITY` account per tenant and currency (`FX Trading <CCY>`, negative balance allowed). Each is created automatically on first use and has a deterministic id.
- **Rate stamp.** Each conversion records its accounts, currencies, both amounts, the rate, the rate's source and as-of time, and its journal entry in a new `fx_conversions` table. The table is append-only, protected by RLS, and its rounding rule is enforced by a database `CHECK`.
- **`GET /api/v1/fx-conversions/`** (newest first, limit 100) and **`GET /api/v1/fx-conversions/{id}/`**.
- **Per-currency balance pre-check in posting.** A journal entry that balances overall but not within each currency is now rejected with `422 unbalanced_entry` before anything is written. Today it fails at commit as a raw `integrity_error`. The database trigger stays as the backstop.
- **Demo:**
  - The seed adds an empty `Cash EUR` account.
  - The page shows each account's currency and gets a "Convert currency" form, a recent-conversions table, and a new step in the guide.
  - Transfers between accounts in different currencies are disabled in the form, which points to the conversion form instead.
  - Hold requests send the selected account's currency instead of a hard-coded `USD`.
  - The existing USD flows and walkthrough numbers are unchanged.
- **BREAKING (error code only):** an entry that is unbalanced within one currency but balanced overall now returns `unbalanced_entry` instead of `integrity_error`. Both are HTTP 422.

## Capabilities

### New Capabilities
- `ledger/fx-conversions`: currency conversion entries, per-currency FX trading accounts, the rate stamp and its rounding rule, history immutability, and tenant isolation.

### Modified Capabilities
- `ledger/posting`: entries must balance within each currency before any write. Today that is enforced only by the commit-time trigger.
- `api/http-gateway`: FX conversion endpoints (create, list, retrieve) with idempotency and error codes.
- `demo-ui`:
  - The guide covers the conversion flow.
  - Each account's currency is shown.
  - The transfer form blocks cross-currency transfers.
  - Holds send the account's currency.
  - New conversion form and recent-conversions table.
  - The polish-era "Request behavior unchanged" requirement is removed, because this change intentionally alters the hold request body.

## Impact

- **Database:**
  - Migration `0014_fx_conversions`: the `fx_conversions` table (RLS, immutability triggers, rounding `CHECK`, foreign keys to the entry and both accounts) plus grants for `ledger_runtime`.
  - No changes to existing tables.
- **Code:**
  - `ledger/models.py`: `FxConversion`.
  - `ledger/services/fx.py` (new): conversion service. It follows `holds.capture_hold`'s pattern of an outer idempotent transaction that calls `post_entry` with a derived key.
  - `ledger/services/posting.py`: per-currency pre-check.
  - `api/serializers.py`, `api/views.py`, `api/urls.py`, `api/exceptions.py`.
  - `ledger/management/commands/seed_demo.py`.
  - `api/templates/demo/index.html`.
- **Tests:**
  - New service and API test modules.
  - `tests/test_demo_bootstrap.py` expects 5 seeded accounts instead of 4.
- **Docs:** README Core Design section, API section, demo walkthrough step, and roadmap tick.
- **Compatibility:** additive apart from the error-code change above. Existing entries, statements, balances, holds, sharding, period close and replica routing are unaffected. A conversion is an ordinary journal entry, so it shows up in statements, webhooks (`entry.posted`), reconciliation and reversals with no special handling.
- **Out of scope:**
  - Rate feeds or a stored rate table (the client supplies the rate).
  - Revaluation or unrealized FX gain/loss reporting.
  - Multi-hop or cross-tenant conversions.
  - Using an FX trading account as a conversion's source or destination (rejected).
  - Adding currency to webhook line payloads.
