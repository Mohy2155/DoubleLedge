# Proposal

## Why

Account balance queries currently rely on scanning and aggregating raw `transaction_lines`. As ledger volume increases, balance reads degrade linearly ($O(N)$).
To achieve high-throughput balance reads without compromising ACID guarantees, we need an append-only, point-in-time balance snapshot system.

## What Changes

- **Immutable Snapshot Storage (`balance_snapshots`)**: A new append-only table recording point-in-time balance checkpoints per account, fenced on `journal_entries.sequence` and `entry_id`, with composite tenant foreign keys and RLS.
- **Read-Optimized Balance Query Engine (`BalanceQueryService`)**: Computes balance by querying the latest *valid* snapshot and aggregating un-checkpointed `transaction_lines`. The engine accumulates raw debits and credits first, applying polarity (ASSET/EXPENSE = Debit - Credit) only at the final step.
- **Temporal & Current Balance API (`/api/v1/accounts/{id}/balance/`)**: Update the *existing* endpoint to use the snapshot engine. `?as_of=<iso8601>` keeps its current meaning (business time, `effective_at`).
- **Asynchronous Checkpoint Worker**: A Celery task (`create_account_balance_snapshots`) that, per tenant and per account, takes a brief row lock on the account (`FOR SHARE ... SKIP LOCKED`) so no in-flight posting can be missed, then writes a snapshot. This replaces a time-based buffer, which only approximates safety.
- **Schema**: Add `sequence BIGINT GENERATED ALWAYS AS IDENTITY` to `journal_entries`. The `transaction_lines` table is **not** renamed.
- **Snapshot verification**: Extend reconciliation to recompute each account's latest snapshot from lines.

## Capabilities

### New Capabilities
- `ledger/snapshots`: Point-in-time balance checkpoints fenced by entry sequence.

### Modified Capabilities
- `api/http-gateway`: `/api/v1/accounts/{id}/balance/` served via snapshots.
- `ledger/reconciliation`: Verifies the latest snapshot per account.

## Impact

- **Database Schema**: Add `sequence` to `journal_entries`. Create `balance_snapshots` with `NUMERIC(28,4)` aggregate columns, composite FKs, and RLS. Existing columns stay `NUMERIC(19,4)`.
- **Service Layer**: Implement `BalanceQueryService`.
- **API Tier**: View and serializer output `Decimal` strings.
- **Background Tasks**: Celery snapshot task running per tenant via the existing tenancy helper.
