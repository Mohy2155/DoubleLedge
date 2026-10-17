# Design

## Context

See proposal.md for motivation. Current-state facts that shape the approach:

- **Accounts are single-currency** (`accounts.currency char(3)`). The deferred triggers `trg_entry_balanced_lines` and `trg_entry_balanced_entry` (migration 0001) reject entries unbalanced within any currency (`GROUP BY a.currency`).
  - `post_entry`'s own pre-check only sums debits against credits across all currencies. A USD/EUR mismatch therefore reaches commit and surfaces through the generic `IntegrityError` handler as 422 `integrity_error` with the raw trigger message.
- **`post_entry` already does most of the work:**
  - idempotency
  - `effective_at` and closed-period checks
  - shard routing
  - locking accounts in sorted id order
  - balance updates, with the `CHECK (allow_negative OR balance >= 0)` constraint mapping to `insufficient_funds`
  - the `entry.posted` outbox event
- **`holds.capture_hold` is the established pattern** for a domain operation that posts an entry. An outer `@retry_on_deadlock @transaction.atomic` function owns the client's `Idempotency-Key` and stores its own response, then calls `post_entry` with a derived key. Direction helpers (`_direction_to_decrease` / `_direction_to_increase`) live in `holds.py`.
- **Shard ids are deterministic:** `uuid5(master_id, "shard_<i>")`.
- **New tables follow migration 0013:**
  - `ENABLE` / `FORCE ROW LEVEL SECURITY`
  - a `tenant_isolation_*` policy on `app.tenant_id`
  - `block_modification()` triggers for UPDATE, DELETE and TRUNCATE
  - a conditional `GRANT SELECT, INSERT ... TO ledger_runtime`
- **`ledger_runtime` can already INSERT into `accounts`** (migration 0012).
- **Python's default `Decimal` context has 28 significant digits.** Multiplying a `numeric(19,4)` amount by a 12-decimal rate can exceed that and silently round. For example, `999999999999999.9999 × 1234567890123456.123456789012` loses digits at 28 digits of precision.
- **The demo hard-codes USD in three places:**
  - the "Amount (USD)" label on the transfer form
  - the "Amount (USD)" label on the hold form
  - `currency: 'USD'` in the hold request body
- **`tests/test_demo_bootstrap.py` asserts exactly 4 seeded accounts.**

## Goals / Non-Goals

**Goals:**
- Conversions are ordinary journal entries. Statements, balances, snapshots, reversals, webhooks, reconciliation and replica routing handle them with no special cases.
- The database alone keeps every currency balanced and keeps the rate stamp consistent. The service only adds friendlier errors.
- The only cost to existing posting paths is one per-currency loop over already-locked accounts.

**Non-Goals:**
- Valuing FX trading account positions in a reporting currency (unrealized gain/loss).
- Load-spreading for hot trading accounts. The existing sharding could be applied later.
- A database trigger cross-checking the stamp against the entry's lines (see Risks).

## Decisions

### D1. Currency trading accounts, four lines per conversion

A conversion of `X` from currency A to `Y` in currency B posts four lines:
1. decrease the source by `X`
2. offset that in `FX Trading A`
3. offset `Y` in `FX Trading B`
4. increase the destination by `Y`

"Decrease" and "increase" use the account's normal balance side, through the holds direction helpers, which move to `posting.py` so both services share them. Each currency nets to zero, so the existing per-currency invariant needs no change.

*Rejected alternatives:*
- **Relaxing the invariant for "FX entries" with a two-line, cross-currency entry.** It weakens the ledger's core guarantee.
- **A single currency-less clearing account.** Accounts are single-currency by schema.
- **Requiring clients to pass their own trading accounts.** It adds friction, and different clients would pick inconsistent accounts.

### D2. Deterministic, lazily created trading accounts

`fx_trading_account_id(tenant_id, ccy) = uuid5(tenant_id, "fx_trading:<CCY>")`. This uses the same pattern as shard ids.

Inside the conversion transaction, before `post_entry` locks anything:
- The service inserts the missing trading accounts with `INSERT ... ON CONFLICT (id) DO NOTHING`.
- The accounts are `EQUITY`, `allow_negative = true`, named `FX Trading <CCY>`.
- Both rows go in one statement, in ascending id order. Two concurrent first conversions then wait on the same row order instead of deadlocking. `retry_on_deadlock` covers anything left over.

The same id function also detects trading accounts used as source or destination (`invalid_account`).

*Rejected alternatives:*
- **Looking them up by name.** Names aren't unique and can be edited.
- **A new `accounts.system_role` column.** It is a schema change on the hottest table, for something the id already identifies.

### D3. Service shape: `ledger/services/fx.py`, mirroring `capture_hold`

`convert(tenant, payload, idempotency_key, request_hash)` is decorated with `@retry_on_deadlock @transaction.atomic`. It runs these steps:
1. **Idempotency:** `get_or_insert_idempotency_key`, then replay the stored response or raise `idempotency_key_reuse`.
2. **Load accounts:** load both accounts under RLS; a missing account gives 404.
3. **Validate:** check rate, currencies, trading accounts and `rate_as_of` (D5).
4. **Compute `to_amount`** (D4).
5. **Provision** the trading accounts (D2).
6. **Post the entry:** `post_entry(tenant, {reference_id, description, effective_at, lines}, "fx_conversion:<idempotency_key>", hash)`. This inherits the period, balance, shard and outbox rules.
7. **Record the stamp:** insert the `fx_conversions` row with the returned entry id.
8. **Store the response** on the client's idempotency key.

Errors use a new `FxError` with its own mapping in `api/exceptions.py`, the same way as `HoldError`. `PostingError` and `IntegrityError` from `post_entry` keep their existing mappings.

### D4. Rounding: half-up to 4 places, computed in a wide context, enforced by a `CHECK`

- **Computation:** `to_amount = (from_amount × rate).quantize(0.0001, ROUND_HALF_UP)` inside a `localcontext(prec=60)`.
- **Database:** `CHECK (to_amount = round(from_amount * rate, 4))`. Postgres `round(numeric)` rounds half away from zero, which is the same as half-up for these strictly positive values, and numeric multiplication is exact.
- **Limits:** a `to_amount` of 0, or one that doesn't fit `numeric(19,4)`, gives `invalid_amount`.
- *Rejected:* half-even. Postgres has no built-in half-even `round(numeric)` to enforce it with.

### D5. Validating the rate and `rate_as_of`

- **`rate`:** the serializer passes it through unparsed. The service requires a `str` that parses as a `Decimal`, is greater than 0, has at most 12 decimal places and at most 16 integer digits (`numeric(28,12)`). Anything else gives `invalid_rate`. Every invalid rate, including a JSON number, therefore returns the same 422 code. Doing this in the serializer would turn some of these cases into DRF's 400.
- **`from_amount`:** uses the existing `StrictStringDecimalField` and the posting precision rules.
- **`rate_as_of`:** uses `AwareDateTimeField`. It defaults to `timezone.now()`, and a future value gives `invalid_rate_as_of`.
- **`rate_source`:** optional, at most 64 characters, defaults to `''`.

### D6. The `fx_conversions` table (migration 0014)

```sql
id uuid PK, tenant_id uuid NOT NULL REFERENCES tenants(id),
journal_entry_id uuid NOT NULL UNIQUE, from_account_id uuid NOT NULL, to_account_id uuid NOT NULL,
from_currency char(3) NOT NULL, to_currency char(3) NOT NULL CHECK (from_currency <> to_currency),
from_amount numeric(19,4) NOT NULL CHECK (from_amount > 0),
to_amount numeric(19,4) NOT NULL CHECK (to_amount > 0),
rate numeric(28,12) NOT NULL CHECK (rate > 0),
rate_source text NOT NULL DEFAULT '', rate_as_of timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
CHECK (to_amount = round(from_amount * rate, 4)),
FOREIGN KEY (tenant_id, journal_entry_id) REFERENCES journal_entries (tenant_id, id),
FOREIGN KEY (tenant_id, from_account_id)  REFERENCES accounts (tenant_id, id),
FOREIGN KEY (tenant_id, to_account_id)    REFERENCES accounts (tenant_id, id)
```

- **Supporting objects:** an index on `(tenant_id, created_at DESC)`, RLS with a policy and `block_modification` triggers as in 0013, and the conditional grant.
- **Composite foreign keys:** they make cross-tenant references impossible at the database level.
- **Copied currencies:** keep the stamp self-contained.
- **Model:** `ledger.models.FxConversion`, with `managed = False` like the others.

### D7. Per-currency pre-check in `post_entry`

`post_entry` already holds `accounts_by_id` after locking. It sums each line's signed amount by account currency and raises `PostingError("unbalanced_entry")` if any currency is non-zero. This happens before the `JournalEntry` insert, so the transaction rolls back and the idempotency row goes with it.

The existing all-currency check stays where it is, because it runs before any account read.

### D8. API

- **Routes:** `FxConversionViewSet` is registered at `fx-conversions`, with `create` / `list` / `retrieve`, each under `@tenant_transaction()`. `create` requires `Idempotency-Key` and sets `Idempotent-Replayed` on replay, like `HoldViewSet`.
- **Response:** the stamp fields, plus `effective_at`, `journal_entry_id` and `lines`. Each line carries `currency`, and its `account_id` maps shards to their master, as in `post_entry`.
- **List and retrieve:** read `fx_conversions` and the entry's lines.
- **Replica routing:** these endpoints are not routed (`api/read-replicas` routes only statement and balance).

### D9. Demo

- **Seed:** `seed_demo` adds an empty `Cash EUR` with `get_or_create`, so existing seeds rerun idempotently. The USD seed is unchanged.
- **Accounts table:** gains a currency column.
- **Amount labels:** read the selected account's currency.
- **Transfer form:** validation also disables Post when the two accounts' currencies differ, with a hint pointing to Convert.
- **Hold request:** sends `currency` taken from the selected account.
- **New "Convert currency" card:**
  - fields: From, To, Amount, Rate, Rate source (default `demo`)
  - submission is disabled until the accounts are set and in different currencies
  - it sends strings and a `crypto.randomUUID()` idempotency key
  - on success it shows `from_amount from_ccy → to_amount to_ccy @ rate` exactly as returned, then refreshes accounts, conversions, and the open statement if it was affected
- **"Recent conversions" table:** from `GET /api/v1/fx-conversions/`.
- **Guide:** gains step 5.
- **No client-side preview of `to_amount`:** the page shows only the API's values, per the precision requirement.

## Risks / Trade-offs

- **[Hot trading accounts]** Every conversion in a currency locks that currency's trading account row, so conversions are serialized per tenant and currency. → Acceptable at current scale. The existing hot-account sharding can later be applied to trading accounts without API changes.
- **[The stamp is checked against the lines only by the service]** A buggy caller of the model could stamp an entry with mismatched lines. → `fx_conversions` rows are written only by `fx.convert`. Tests assert that the lines match the stamp, and the rounding and tenant-scoping `CHECK`s and foreign keys are enforced by the database. A trigger cross-checking lines through shard parents was judged not worth the complexity.
- **[The client supplies the rate]** A wrong rate posts a wrong but balanced conversion. → The stamp (`rate_source`, `rate_as_of`) makes it auditable, and reversing the entry undoes it. Rate feeds are out of scope.
- **[Decimal overflow or rounding]** → A wide local context, plus the `numeric(19,4)` fit check that gives `invalid_amount`.
- **[Error-code change]** Per-currency imbalance now returns `unbalanced_entry` instead of `integrity_error`. → Both are 422, and only already-invalid requests are affected. The change is documented in the README.
- **[Trading accounts appear in account lists and the demo]** → Intended: they make the per-currency balancing visible. They are named `FX Trading <CCY>`.

## Migration Plan

- **Rollout:** migration 0014 is additive. Deploy it with the code, since the new endpoint is the only reader or writer of the table.
- **Rollback:** reverse the migration, which drops `fx_conversions`. Conversion entries and trading accounts stay as ordinary ledger history.
- **Demo data:** existing demo tenants gain `Cash EUR` only if `seed_demo` is rerun.
