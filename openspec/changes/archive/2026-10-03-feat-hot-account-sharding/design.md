# Design

## Context

See proposal.md for motivation. We need to shard hot accounts to bypass row-lock contention, while keeping sharding fully transparent: every tenant-facing read (balance, available balance, journal entry lines, statements, webhook payloads) must show the single logical account the tenant created, never an internal shard id.

## Goals / Non-Goals

**Goals:**
- Break the single-row lock TPS ceiling for hot accounts.
- Transparently route fresh postings and hold captures to shards.
- Transparently aggregate `posted_balance`, `pending_holds`, `available_balance`, historical `as_of` balance, and statement lines across master + shards.
- Never expose a physical shard id to a tenant, in any response or webhook payload.
- Provide a periodic, idempotent rollup task to keep shard balances from growing indefinitely.
- Reuse existing invariants (RLS, composite FKs, immutability triggers, deterministic lock ordering, idempotency) unmodified wherever the shard-as-ordinary-account-row property already makes them correct.

**Non-Goals:**
- Dynamic resharding.
- Recursive sharding (a shard cannot itself be sharded).
- A tenant-facing account/shard-creation endpoint (see proposal's Open Question).
- Redis or any external coordination for routing or locking.

## Decisions

### Decision 1: Schema extension on `accounts` (no new table)

```sql
ALTER TABLE accounts
    ADD COLUMN shard_count       INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN parent_account_id UUID;

ALTER TABLE accounts
    ADD CONSTRAINT chk_shard_count_nonneg     CHECK (shard_count >= 0),
    ADD CONSTRAINT chk_shard_count_bounded    CHECK (shard_count <= 64),  -- starting safety rail, not a hard product limit
    ADD CONSTRAINT chk_no_nested_sharding     CHECK (NOT (shard_count > 0 AND parent_account_id IS NOT NULL)),
    ADD CONSTRAINT fk_parent_account_tenant   FOREIGN KEY (tenant_id, parent_account_id)
        REFERENCES accounts (tenant_id, id);
```

- The self-referencing FK is **composite and tenant-scoped**, matching the project's existing cross-tenant-protection pattern (`transaction_lines (tenant_id, account_id) → accounts (tenant_id, id)`). A plain `REFERENCES accounts(id)` would let a shard's `parent_account_id` point at a master in a different tenant; the composite form makes that physically impossible, consistent with how the rest of the schema is built. It relies on the existing `UNIQUE (tenant_id, id)` constraint on `accounts`.
- `parent_account_id` is nullable; Postgres's default `MATCH SIMPLE` FK mode treats a NULL `parent_account_id` as satisfying the constraint trivially (RLS is unaffected since this is an ordinary FK check, not a cross-tenant read).
- **No new table is created**, so this change needs **zero new `GRANT` statements** — shards are rows in a table the app role already has full CRUD on.
- Shard rows copy `tenant_id`, `name` (suffixed, e.g. `f"{master.name} (shard {i})"`), `type`, and `currency` from the master at creation time. However, **all generated shard rows must strictly be provisioned with `allow_negative=True` at the database level**, completely ignoring the master's boolean for the sub-rows. This is required because if a shard is swept to $0 and then a historical transaction against it is reversed, the shard's local balance will momentarily drop negative, which is mathematically correct and necessary for post-sweep reversals.

### Decision 2: Account provisioning (internal, atomic)

`ledger/services/sharding.create_sharded_account(tenant, *, name, type, currency, allow_negative, shard_count)`:

1. In one `transaction.atomic()` block: insert the master row with `shard_count=0` (not yet routable), bulk-create `shard_count` shard rows with `id = uuid.uuid5(master.id, f"shard_{i}")` for `i in range(shard_count)` and `parent_account_id = master.id`, then `UPDATE` the master's `shard_count` to its final value.
2. Because this is a single transaction, no partially-sharded master is ever visible to a concurrent `post_entry` dirty read — either the whole set commits or none of it does.
3. Exposed via a management command (`manage.py create_sharded_account`) for now, per the proposal's Open Question. If a public creation endpoint already exists, wrap this same service function rather than duplicating the logic.

### Decision 3: Transparent routing in `post_entry` (write path)

1. **Dirty read**: `SELECT id, shard_count FROM accounts WHERE id IN (payload_account_ids)`, non-locking.
2. **Routing**: for any id with `shard_count > 0`, rewrite that line's `account_id` to `uuid.uuid5(master_id, f"shard_{random.randint(0, shard_count - 1)}")`.
3. **Why the dirty read is safe**: `shard_count` is set exactly once, inside the atomic block in Decision 2, and never changes afterward (dynamic resharding is a Non-Goal). There is no window where a concurrent read can observe a torn or stale `shard_count`.
4. **Locking**: `select_for_update().filter(id__in=resolved_ids).order_by("id")` runs on the **resolved** (post-routing) ids exactly as today, preserving the deterministic lock-order deadlock mitigation.
5. **Reversals are not routed.** A reversal copies the original entry's lines verbatim, including their already-resolved `account_id` (a shard id, which has `shard_count = 0` and so is never rewritten by step 2). This is what makes a reversal net out against the exact shard the original line touched, rather than a newly and independently chosen shard — required both for correctness (per-shard balances stay accurate between rollups) and for audit traceability. Add an explicit test for this; do not rely on it being incidental.
6. **Hold captures** build a fresh line the same way a normal posting does, so they go through steps 1–4 like any other new line — no special-casing needed.
7. A single entry may contain two lines that both resolve to the same master id (e.g. two correction lines against the same hot account); each line routes independently, which is correct in aggregate even though it may touch two different shards within one entry.

### Decision 4: Aggregate reads — balance, available balance, and holds (read path, part 1)

**Problem the previous draft missed:** after routing, `accounts.balance` on the master row only reflects whatever rollups have swept into it so far — it is stale (and, before the first rollup, zero) at any point between sweeps. Any code that reads a sharded master's own `balance` column directly, or validates a hold against it, is wrong regardless of rollup cadence.

**Decision:** `ledger/services/sharding.resolve_member_ids(account) -> list[UUID]` returns `[master.id, *shard_ids]` for a sharded master, or `[account.id]` for a plain account. A new `ledger/services/sharding.get_aggregate_balance(account, as_of=None)` is the single source of truth for a (possibly sharded) account's money figures:

- `posted_balance` (no `as_of`): `SUM(Account.balance) WHERE id IN resolve_member_ids(account)`.
- Historical `as_of`: `SUM(BalanceQueryService.compute(member_id, as_of) for member_id in resolve_member_ids(account))` — reuses the existing snapshot-plus-delta engine per member id unmodified.
- `pending_holds`: `SUM(Hold.amount - Hold.captured_amount) WHERE status = 'PENDING' AND account_id = account.id`. **Holds always reference the master id directly** — a hold is never created against, nor routed to, a shard. This keeps hold semantics simple and matches how a hold's `destination_account_id` on capture is just another posting line that gets routed normally per Decision 3.
- `available_balance = posted_balance - pending_holds`.

**Required call sites:**
- `GET /api/v1/accounts/{id}/` (the `AccountSerializer`'s `posted_balance` / `pending_holds` / `available_balance` fields).
- `GET /api/v1/accounts/{id}/balance/?as_of=` (the existing historical-balance endpoint from the snapshots change).
- Hold creation/validation, **if** `ledger/services/holds.py` checks available balance before authorizing a PENDING hold — confirm this against the current implementation; if it does, it must call `get_aggregate_balance`, not read `account.balance`.

This is why a shard's own `allow_negative` does not misrepresent anything to the tenant: the CHECK constraint on each shard row is an internal concurrency-safety rail (prevents a single shard from going negative mid-stream), while the tenant-visible `available_balance` is always the aggregate figure.

### Decision 5: Logical-id mapping (read path, part 2)

**Problem the previous draft missed entirely:** shard ids are only hidden from the balance endpoints. `TransactionLine.account_id` is also rendered directly in `GET /api/v1/journal-entries/{id}/`, in statement lines, and in outbox/webhook payloads built inside `post_entry`. Without translation, a tenant posting to a sharded "platform fee" account would see an unfamiliar, effectively random account id on every entry and webhook, breaking the "transparent" promise and breaking any client-side reconciliation keyed on account id.

**Decision:** a single helper, `ledger/services/sharding.to_logical_account_id(account_id, account_lookup) -> UUID`, returns `parent_account_id` if the row is a shard, else the id unchanged. Every place an `account_id` is serialized for a tenant-facing response or an outbox payload calls this helper, batching the lookup (`Account.objects.filter(id__in=...).values('id', 'parent_account_id')`) rather than querying per line:

- `TransactionLineSerializer` / `JournalEntrySerializer` read path.
- Account-statement line serialization.
- `OutboxEvent.payload` construction in `post_entry` (so `entry.posted` / `entry.reversed` webhook deliveries show the logical id).

The physical shard id is purely an internal storage/locking detail after this change; it never appears in anything the tenant can read.

**Corollary:** `GET /api/v1/accounts/{id}/` must return `404` when `{id}` itself resolves to a row with `parent_account_id IS NOT NULL`. A shard is not an independently addressable resource, even to its own tenant — guessing a shard's deterministic `uuid5` must not grant a second, partial view of the same money.

### Decision 6: Statement aggregation (read path, part 3)

For a sharded master, `GET /accounts/{id}/statement/` must merge `transaction_lines` across all member ids (Decision 4's `resolve_member_ids`) into one chronologically ordered, correctly cursor-paginated stream with a single **aggregate** running balance — not each shard's own running total.

- Order by a stable total-order tuple over the unioned rows (confirm and reuse whatever field(s) the existing single-account statement already keys its cursor on — likely `(posted_at, line id)` — before implementing; do not invent a new ordering scheme that diverges from the existing endpoint's pagination contract).
- Compute the aggregate running balance with a window function over the unioned set, e.g. `SUM(signed_amount) OVER (ORDER BY posted_at, id)`, seeded by the same starting-balance mechanism the current single-account implementation already uses for its first page.
- Apply Decision 5's logical-id mapping to every line in the merged output.
- A rollup entry (Decision 7) touches the master id in one line and a shard id in another within the same journal entry; both lines will appear in the merged statement, debit and credit, as a normal two-line entry would. This is correct ledger data, not a bug, but is worth flagging to whoever reviews statement UX — a tenant will occasionally see small internal transfer pairs they didn't originate.

### Decision 7: Rollup sweep task

A periodic Celery task, run **per tenant** via the existing `ledger/services/tenancy.for_each_tenant` helper (the same RLS-context requirement that applies to every other Celery task touching tenant-scoped tables — a task with no `app.tenant_id` set sees zero rows under `FORCE ROW LEVEL SECURITY` and would silently do nothing):

1. For each tenant, find shards with non-zero `balance`.
2. For each such shard, call `post_entry(tenant, payload, bypass_sharding=True)` moving the shard's full balance to its master: debit the shard, credit the master, both as **already-resolved** ids (so Decision 3's routing step does not re-route the master side back into a different shard — `bypass_sharding=True` skips step 2 of Decision 3 entirely for this call).
3. Give the rollup entry a **deterministic `reference_id`**, e.g. `f"rollup:{shard.id}:{shard.version}"` or another value derived from state that changes between sweeps — not a timestamp alone, and not omitted. `journal_entries` enforces `UNIQUE (tenant_id, reference_id)`, so a retried or duplicated sweep (overlapping beat ticks, a second worker) hits the existing idempotency path and no-ops instead of double-moving money. This is required; the previous draft didn't specify a `reference_id` at all, which would either error (missing field) or, worse, pick a colliding/non-deterministic value.
4. Correctness under concurrency needs no new locking scheme: `post_entry` already takes `FOR UPDATE` locks on both the shard and master rows (sorted, as always), so a sweep racing a live posting to the same shard is handled by the existing mechanism — unlike the balance-snapshot task, there is no need for a `FOR SHARE SKIP LOCKED` read trick here.
5. As defense in depth against wasted work (not correctness — step 3 already guarantees that), guard each tenant's sweep with `pg_try_advisory_lock`, consistent with how other periodic tenant-scoped tasks in this codebase avoid overlapping runs.

### Decision 8: Reconciliation and snapshots need no code change

Both `ledger/reconciliation` and `ledger/snapshots` operate per physical account row and are already correct for shard rows and the master row with no special-casing, because a shard is an ordinary row in `accounts` with its own ordinary lines in `transaction_lines`. The only thing to verify (not change) is that reconciliation's account iteration doesn't filter on `parent_account_id IS NULL` anywhere. An optional, purely informational aggregate cross-check (`SUM(master.balance, *shard.balance)` reported per sharded master) can be added to the reconciliation report for operator visibility but is not required for the per-row invariant to hold.

### Decision 9: Mutually Exclusive Capabilities (ADR)

**Problem:** To strictly enforce `allow_negative=False` in real-time, the system *must* lock a single aggregate row to serialize transactions. By splitting the lock across $N$ shards, we destroy the serialization required to prevent an overdraft race condition. If 10 concurrent threads attempt a $100 debit on a sharded account with a $100 balance, they would all see a positive aggregate balance, lock separate physical shards, and commit, resulting in a fatal double-spend.

**Decision:** We formally establish that `shard_count > 0` and `allow_negative = False` are mathematically incompatible.
- Hot-account sharding is explicitly designed for high-volume platform accounts (e.g., global Revenue, holding accounts) where balances naturally fluctuate and real-time strict limits are not required. 
- Accounts requiring strict real-time overdraft protection must remain unsharded to preserve synchronous `SELECT FOR UPDATE` locks.

This invariant is strictly enforced at the database level via a raw SQL `CHECK` constraint: `CHECK (NOT (shard_count > 0 AND allow_negative = false))`. It is also enforced at the application boundary via a 400 Bad Request to provide clean client UX.

## Risks / Trade-offs

- **[Risk] Extra query overhead in `post_entry`** → Mitigation: the dirty read is an indexed PK lookup; safe and cheap given `shard_count` is write-once (Decision 3).
- **[Risk] Available balance silently wrong between rollups** → Mitigation: Decision 4 makes the aggregate helper the only path to a money figure; add a test that creates a hold against a sharded account *before any rollup has run* and asserts `available_balance` is still correct.
- **[Risk] Shard ids leaking through entry/statement/webhook responses** → Mitigation: Decision 5's mapping, applied at every serialization point, plus a test that posts to a sharded account and asserts no shard id appears anywhere in the HTTP response or the resulting webhook payload.
- **[Risk] Reversal drifting a shard's balance by re-routing** → Mitigation: Decision 3 step 5, plus an explicit test.
- **[Risk] Statement pagination correctness under concurrent rollups** → Mitigation: Decision 6's stable ordering tuple is independent of which shard a line physically lives on; a rollup changes shard balances, not line timestamps or ids, so an in-flight cursor remains valid.
- **[Risk] Duplicate/overlapping rollup runs** → Mitigation: Decision 7 steps 3 and 5 (idempotent `reference_id` as the real guarantee, advisory lock to avoid wasted work).
- **[Risk] A guessed shard id used as a direct account lookup** → Mitigation: Decision 5's corollary (404 on shard ids).
