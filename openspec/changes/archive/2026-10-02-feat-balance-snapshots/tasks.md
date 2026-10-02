# Tasks

## 1. Schema & Core DB Model

- [ ] 1.1 Create `0007_balance_snapshots.py` (raw SQL, same style as 0005) to add `sequence BIGINT GENERATED ALWAYS AS IDENTITY` to `journal_entries` and create `balance_snapshots` per design (composite FKs, `max_effective_at`, RLS, immutability triggers), with `GRANT SELECT, INSERT` to the same app role name used by earlier migrations. Do **not** rename `transaction_lines`. Verify `python manage.py migrate` succeeds and that `post_entry` still posts and the commit-time balance trigger still fires.
- [ ] 1.2 Add the `BalanceSnapshot` model (`managed = False`, matching all other ledger models and the RunSQL-created tables; never `managed = True`, which would generate a duplicate `CREATE TABLE`). Map `JournalEntry.sequence` only in a way that is excluded from INSERTs (or leave it unmapped and read via raw SQL). Verify `pytest tests/test_models.py tests/test_posting.py`, plus cross-tenant insert/read on `balance_snapshots` fails as the app role.

## 2. Balance Query Engine

- [ ] 2.1 Implement `BalanceQueryService` as a single SQL statement: latest eligible snapshot (`max_effective_at <= as_of`) plus delta lines (`sequence > snapshot.sequence`, `effective_at <= as_of`), accumulating debits and credits first, then applying polarity. Verify: asset and liability polarity; account with no lines; back-dated and future-dated entries; a future-dated entry forces fallback to an older snapshot with the same result as a full scan; current balance (no `as_of`) equals `accounts.balance`.
- [ ] 2.2 Verify `NUMERIC(28,4)` Decimal precision end to end (no floats in tests) using values beyond 19 digits in totals.

## 3. Asynchronous Snapshot Task

- [ ] 3.1 Implement Celery task `create_account_balance_snapshots` using `ledger.services.tenancy.for_each_tenant`: per account, in its own short transaction, lock the account row `FOR SHARE SKIP LOCKED`, aggregate, and insert (`ON CONFLICT DO NOTHING`); apply `SNAPSHOT_MIN_DELTA_LINES`. Verify with a concurrency test that deliberately commits two postings out of sequence order (holding one transaction open) and shows no entry is ever missing from snapshot totals, and that a locked account is skipped rather than blocking.
- [ ] 3.2 Add Celery beat schedule and verify idempotent execution (second run inserts no rows).

## 4. API Tier

- [ ] 4.1 Create `AccountBalanceSerializer` outputting string decimals (`max_digits=28`).
- [ ] 4.2 Update the **existing** `GET /api/v1/accounts/{id}/balance/` view (do not add a second route) to use `BalanceQueryService`. Verify the response contains `snapshot_used` and string-formatted balance/debits/credits, `as_of` still requires a timezone-aware timestamp, and cross-tenant access returns 404.

## 5. Reconciliation & Verification

- [ ] 5.1 Extend the reconciliation engine to recompute the latest snapshot per account from `transaction_lines` and flag mismatches in `ReconciliationReport`. Verify with a test that tampers a snapshot row (as the owner role) and is detected.
- [ ] 5.2 Run all tests `pytest tests/ -v`, ensuring existing posting, reconciliation, and API tests still pass.
