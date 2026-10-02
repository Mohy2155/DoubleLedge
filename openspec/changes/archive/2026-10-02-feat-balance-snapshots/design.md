# Design

## Context

Current balance calculation scans `transaction_lines`. We need an immutable point-in-time balance snapshot system that cannot skip out-of-order commits, enforces zero-float numeric precision, and computes polarity without drifting.

## Goals / Non-Goals

**Goals:**
- Provide $O(1)$ to $O(k)$ account balance queries by caching snapshots and scanning delta `transaction_lines`.
- Add a sequence fence on `journal_entries.sequence`.
- Aggregate total debits and credits *before* applying polarity.
- Snapshot aggregate columns use `NUMERIC(28,4)` (wider than the `NUMERIC(19,4)` line amounts so sums cannot overflow). Existing columns are unchanged. No floats.
- Make snapshots provably complete, not probably complete (see Decision 2).
- Strict RLS and composite tenant foreign keys on snapshot tables; grants only `SELECT, INSERT` to the app role.

**Non-Goals:**
- Renaming `transaction_lines`. The name is used by the README, the `assert_entry_balanced()` function body (plpgsql binds table names at runtime, so a rename would break commit-time balance checks), reconciliation SQL, models, and tests. It is cosmetic churn with real risk.
- Pruning or compacting snapshots (append-only for now).

## Architecture & Data Model

### Schema Updates
1. Add `sequence BIGINT GENERATED ALWAYS AS IDENTITY` to `journal_entries`.
2. The Django `JournalEntry` model must **not** write this column: with `GENERATED ALWAYS`, any explicit value (even NULL) makes `INSERT` fail. Either leave it unmapped and read it via raw SQL, or map it so it is excluded from inserts. `post_entry` must keep working unchanged.

### Data Schema (`balance_snapshots`)

```sql
CREATE TABLE balance_snapshots (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid NOT NULL REFERENCES tenants(id),
    account_id         uuid NOT NULL,
    entry_id           uuid NOT NULL,
    sequence           bigint NOT NULL,           -- max entry sequence included for this account
    max_effective_at   timestamptz NOT NULL,      -- max effective_at over all included entries
    balance            numeric(28, 4) NOT NULL,   -- polarity applied
    debit_total        numeric(28, 4) NOT NULL,
    credit_total       numeric(28, 4) NOT NULL,
    line_count         bigint NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, account_id, sequence),
    FOREIGN KEY (tenant_id, account_id) REFERENCES accounts (tenant_id, id),
    FOREIGN KEY (tenant_id, entry_id)   REFERENCES journal_entries (tenant_id, id)
);
-- UNIQUE (tenant_id, account_id, sequence) already serves "latest snapshot" lookups
-- via a backward index scan; no separate DESC index is needed.

ALTER TABLE balance_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE balance_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_balance_snapshots ON balance_snapshots
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
```

Attach the same immutability triggers (`BEFORE UPDATE OR DELETE`, `BEFORE TRUNCATE`) used on the ledger tables, in addition to the `SELECT, INSERT`-only grant. The composite FKs rely on the existing `UNIQUE (tenant_id, id)` constraints on `accounts` and `journal_entries`.

### Mathematical Polarity & Hybrid Query Engine

`BalanceQueryService` calculates totals as:
1. `total_debit = snapshot.debit_total + sum(delta.debit)`
2. `total_credit = snapshot.credit_total + sum(delta.credit)`
3. Apply polarity:
   - For `ASSET`, `EXPENSE`: `total_debit - total_credit`
   - For `LIABILITY`, `EQUITY`, `REVENUE`: `total_credit - total_debit`

Run the snapshot lookup and delta aggregation as a single SQL statement (CTE) so they see one consistent view.

### Temporal Query logic (`?as_of=<iso8601>`)
`as_of` is **business time (`effective_at`)**, matching the README and the existing endpoint. Entries can be back-dated or future-dated, so a snapshot (which is cumulative by posting `sequence`) is only usable for a given `as_of` if every entry it includes satisfies `effective_at <= as_of`.
- Select the latest snapshot with `snapshot.max_effective_at <= as_of` (ordered by `sequence DESC`). If none, start from zero.
- Delta: `transaction_lines` joined to `journal_entries` where `sequence > snapshot.sequence` AND `effective_at <= as_of`.
- Without `as_of` (current balance): latest snapshot plus all lines with `sequence > snapshot.sequence`, no time filter. This must equal `accounts.balance`.

A future-dated entry makes later snapshots ineligible for earlier `as_of` values (falls back to an older snapshot), which costs speed but never correctness.

## Decisions

### Decision 1: Snapshot Fencing by `journal_entries.sequence`
* **Choice**: Fence by entry sequence and `entry_id`, never by line IDs.
* **Rationale**: Entries are atomic and balanced at commit, so a sequence boundary never splits an entry.

### Decision 2: Deterministic snapshot safety via the account row lock
* **Problem**: Identity values are allocated at `INSERT` but become visible at `COMMIT`, so a fence on `max(sequence)` can skip an entry that commits later with a lower sequence. Since snapshots are immutable, a skipped entry would be wrong *forever*, and a time buffer (`posted_at <= now() - 1 minute`) only narrows the window: `posted_at` is the transaction start time, so any posting transaction that stays open longer than the buffer breaks it silently.
* **Choice**: The snapshot task locks the account row before computing: `SELECT ... FROM accounts WHERE id = %s FOR SHARE SKIP LOCKED`, then aggregates in a later statement of the same `READ COMMITTED` transaction and inserts the snapshot (`ON CONFLICT DO NOTHING`).
* **Rationale**: `post_entry` locks every touched account (`FOR UPDATE`, sorted) *before* inserting the entry, so the sequence is allocated only while holding the account lock. Therefore, once the snapshot holds the lock: every entry touching the account is either already committed and visible, or has not yet allocated its sequence (and will get one greater than anything visible now). `FOR SHARE` conflicts with the postings' `FOR UPDATE`, so it waits only for the commit of a posting already in flight and blocks new postings to that account for milliseconds. `SKIP LOCKED` means busy accounts are skipped and retried next tick instead of waiting. The lock is held by one account at a time, so it adds no lock-order cycles.
* **Snapshot contents**: `sequence` = max sequence among the account's lines' entries, `max_effective_at` = max `effective_at` over included entries, totals over all the account's lines up to that sequence. Re-running with no new lines inserts nothing (idempotent via the unique key).
* **Trigger criteria**: Only snapshot accounts with at least `SNAPSHOT_MIN_DELTA_LINES` lines since their latest snapshot (setting, default e.g. 1000), to bound table growth.

### Decision 3: Snapshots are verified, not trusted
* **Choice**: Reconciliation recomputes the latest snapshot per account from `transaction_lines` (totals and `line_count` up to `snapshot.sequence`) and reports mismatches in the existing `ReconciliationReport`.
* **Rationale**: Append-only means an incorrect snapshot is never repaired in place; it must be detectable.
