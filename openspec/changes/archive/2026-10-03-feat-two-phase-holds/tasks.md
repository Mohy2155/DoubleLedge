# Tasks

## 1. Migration & DDL

- [x] 1.1 Create `ledger/migrations/0008_holds.py` as a `RunSQL` migration (depends on `0007_balance_snapshots`). The forward SQL must:
  - Create the `holds` table with all columns: `id uuid PK DEFAULT gen_random_uuid()`, `tenant_id uuid NOT NULL REFERENCES tenants(id)`, `reference_id text NOT NULL`, `account_id uuid NOT NULL`, `currency char(3) NOT NULL`, `amount numeric(19,4) NOT NULL CHECK (amount > 0)`, `captured_amount numeric(19,4) NOT NULL DEFAULT 0 CHECK (captured_amount >= 0)`, `status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','CAPTURED','VOIDED','EXPIRED'))`, `description text NOT NULL DEFAULT ''`, `journal_entry_id uuid` (nullable), `expires_at timestamptz NOT NULL`, `created_at timestamptz NOT NULL DEFAULT now()`, `updated_at timestamptz NOT NULL DEFAULT now()`, `metadata jsonb NOT NULL DEFAULT '{}'`, `CHECK (captured_amount <= amount)`, `UNIQUE (tenant_id, reference_id)`, `UNIQUE (tenant_id, id)`.
  - Add composite foreign keys: `FOREIGN KEY (tenant_id, account_id) REFERENCES accounts (tenant_id, id)` and `FOREIGN KEY (tenant_id, journal_entry_id) REFERENCES journal_entries (tenant_id, id)`.
  - Create partial indexes: `idx_holds_pending ON holds (tenant_id, account_id) WHERE status = 'PENDING'` and `idx_holds_pending_expiry ON holds (expires_at) WHERE status = 'PENDING'`.
  - Create the `update_holds_timestamp()` trigger function and `trg_holds_updated_at` trigger (`BEFORE UPDATE ON holds FOR EACH ROW`).
  - Create the `block_terminal_hold_update()` trigger function and `trg_holds_terminal_immutable` trigger (`BEFORE UPDATE ON holds FOR EACH ROW`) that raises an exception if `OLD.status IN ('CAPTURED','VOIDED','EXPIRED')`.
  - Add `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, and the standard fail-closed `tenant_isolation_holds` policy using `nullif(current_setting('app.tenant_id', true), '')::uuid`.
  - Zero `GRANT` statements.
  - The reverse SQL must drop the table, triggers, and functions.
  - Verify: `python manage.py migrate` succeeds, `post_entry` still works, and existing tests pass.

- [x] 1.2 Add the `Hold` model to `ledger/models.py` with `managed = False`, `db_table = 'holds'`, matching the DDL schema exactly. Include `Status` choices enum (`PENDING`, `CAPTURED`, `VOIDED`, `EXPIRED`), `ForeignKey` to `Tenant`, `Account`, and `JournalEntry` (nullable). Add `Meta.constraints` for the two unique constraints (`unique_tenant_hold`, `unique_tenant_hold_reference`). Add the model to the `0008_holds.py` migration using `migrations.CreateModel` with `managed=False` (same pattern as `0002_models.py`). Verify: `makemigrations --dry-run` produces no additional migrations; the model can be imported and queried.

- [x] 1.3 Write `tests/test_holds_rls.py`: verify that with RLS enforced, cross-tenant INSERT and SELECT on `holds` fail (return empty or raise), and that a hold inserted by tenant A is invisible to tenant B. Use the same raw-SQL RLS testing pattern from `tests/test_tenancy.py`.

## 2. Hold Service Layer

- [x] 2.1 Create `ledger/services/holds.py` with `HoldError(Exception)`. Implement `authorize_hold(tenant, payload, idempotency_key, request_hash)`:
  - Call `get_or_insert_idempotency_key` for replay/reject logic.
  - `SET LOCAL app.tenant_id` and `SET LOCAL lock_timeout = '2s'`.
  - `Account.objects.select_for_update().get(tenant_id=tenant.id, id=payload['account_id'])` to lock the account.
  - Validate: `currency` matches account, `expires_at` is in the future, `amount` is a positive string decimal.
  - Compute available balance: `SUM(amount - captured_amount) WHERE status='PENDING'` for the account, then `available = account.balance - pending_total`.
  - If `available - hold_amount < 0` and `not account.allow_negative`, raise `HoldError("insufficient_funds")`.
  - Insert the `Hold` row.
  - Insert an `OutboxEvent` with `type="hold.authorized"`.
  - Record idempotency success.
  - Return `(201, response_body, is_replayed)`.
  - Wrap with `@retry_on_deadlock(max_retries=3, base_delay=0.05)` and `@transaction.atomic`.

- [x] 2.2 Implement `capture_hold(tenant, hold_id, payload, idempotency_key, request_hash)`:
  - Idempotency check.
  - `SET LOCAL app.tenant_id` and lock timeout.
  - `Hold.objects.select_for_update().get(tenant_id=tenant.id, id=hold_id)`.
  - Validate `status == 'PENDING'`; raise `HoldError("hold_not_pending")` otherwise.
  - Compute `remaining = hold.amount - hold.captured_amount`. If `payload['amount']` is provided, validate it is `> 0` and `<= remaining`; otherwise default to `remaining`.
  - Build a balanced posting payload: two lines (hold account and destination account), with directions determined by the hold account's normal side.
  - Call `post_entry(tenant, posting_payload, derived_idempotency_key, posting_hash)` — the derived key is `f"hold_capture:{hold.id}"` to avoid collision with the outer idempotency key.
  - Update hold: `status='CAPTURED'`, `captured_amount=capture_amount`, `journal_entry_id=entry_id`.
  - Insert `OutboxEvent` with `type="hold.captured"`.
  - Record outer idempotency success.
  - Return `(201, response_body, is_replayed)`.
  - Wrap with `@retry_on_deadlock` and `@transaction.atomic`.

- [x] 2.3 Implement `void_hold(tenant, hold_id, idempotency_key, request_hash)`:
  - Idempotency check.
  - `SET LOCAL app.tenant_id` and lock timeout.
  - `Hold.objects.select_for_update().get(tenant_id=tenant.id, id=hold_id)`.
  - Validate `status == 'PENDING'`.
  - Set `status='VOIDED'`, save.
  - Insert `OutboxEvent` with `type="hold.voided"`.
  - Record idempotency. Return `(200, response_body, is_replayed)`.
  - Wrap with `@retry_on_deadlock` and `@transaction.atomic`.

- [x] 2.4 Implement `compute_available_balance(account, pending_holds_total)` as a standalone function in `holds.py`. For all account types: `available = account.balance - pending_holds_total`. Write unit tests verifying correct polarity for each of the five account types (`ASSET`, `LIABILITY`, `EQUITY`, `REVENUE`, `EXPENSE`) and `allow_negative` behavior.

## 3. API Views & Serializers

- [x] 3.1 Add serializers to `api/serializers.py`:
  - `HoldSerializer` (read, all fields as per design).
  - `HoldCreateSerializer` (write, with `StrictStringDecimalField` for `amount`).
  - `HoldCaptureSerializer` (write, `amount` optional, `destination_account_id`, `reference_id`, `description`).
  - Update `WebhookEndpointSerializer`'s `event_types` choices to include `hold.authorized`, `hold.captured`, `hold.voided`, `hold.expired`.

- [x] 3.2 Update `AccountSerializer` to output `posted_balance`, `pending_holds`, and `available_balance` (all `StrictStringDecimalField(max_digits=19, decimal_places=4)`) instead of the current single `balance` field. Update `AccountViewSet.retrieve` to annotate the account with pending holds total before serialization.

- [x] 3.3 Create `HoldViewSet` in `api/views.py`:
  - `create` → `POST /api/v1/holds/` → calls `authorize_hold`. Requires `Idempotency-Key`.
  - `retrieve` → `GET /api/v1/holds/{id}/` → fetches hold by PK and tenant.
  - `capture` → `POST /api/v1/holds/{id}/capture/` → calls `capture_hold`. Requires `Idempotency-Key`.
  - `void` → `POST /api/v1/holds/{id}/void/` → calls `void_hold`. Requires `Idempotency-Key`.
  - All endpoints use the `@tenant_transaction()` decorator.
  - Map `HoldError` codes to HTTP status codes in an exception handler (same pattern as `PostingError`).

- [x] 3.4 Register `HoldViewSet` in `api/urls.py`:
  ```python
  router.register(r'holds', HoldViewSet, basename='hold')
  ```
  Wire the `capture` and `void` actions as detail routes.

## 4. Celery Tasks

- [x] 4.1 Add `expire_holds` task to `workers/tasks.py`:
  - Use `for_each_tenant` to iterate tenants.
  - Per tenant: execute raw SQL `UPDATE holds SET status='EXPIRED', updated_at=now() WHERE id IN (SELECT id FROM holds WHERE status='PENDING' AND expires_at <= now() ORDER BY expires_at LIMIT 500 FOR UPDATE SKIP LOCKED) RETURNING id, tenant_id, account_id, amount, reference_id`.
  - For each expired row, create an `OutboxEvent` with `type="hold.expired"`.
  - Add to Celery Beat schedule (run every 60 seconds).
  - Verify: a hold with `expires_at` in the past transitions to `EXPIRED`; a hold with `expires_at` in the future is untouched; running twice is idempotent.

## 5. Test Coverage

- [x] 5.1 `tests/test_holds.py` — Core hold lifecycle:
  - Authorize a hold and verify the `Hold` row exists with `status='PENDING'`.
  - Capture a hold and verify `status='CAPTURED'`, `journal_entry_id` is set, and the resulting `JournalEntry` is balanced with correct lines.
  - Void a hold and verify `status='VOIDED'`, no `JournalEntry` created.
  - Verify `available_balance` decreases after authorization and increases after void.
  - Verify `available_balance` decreases after authorization and `posted_balance` changes after capture.
  - Verify outbox events: `hold.authorized`, `hold.captured`, `hold.voided` each produce exactly one `OutboxEvent`.

- [x] 5.2 `tests/test_holds.py` — Partial capture:
  - Authorize a hold for 500.00, capture 300.00, verify `captured_amount=300.00`, `released_amount=200.00` in response, and the `JournalEntry` is for 300.00.
  - Attempt to capture more than the remaining amount and verify `422 capture_exceeds_hold`.
  - Capture the full amount and verify `captured_amount == amount`.

- [x] 5.3 `tests/test_holds.py` — Insufficient funds & overdraft:
  - Create an account with `balance=100.00`, `allow_negative=False`. Authorize a hold for 100.00 (succeeds). Attempt to authorize a second hold for 1.00 and verify `422 insufficient_funds`.
  - Create an account with `allow_negative=True`. Authorize a hold exceeding the balance and verify it succeeds.
  - Post an entry that would make `available_balance` negative (balance drops below pending holds) and verify `post_entry` still succeeds (posting doesn't check holds — only `authorize_hold` checks available balance).

- [x] 5.4 `tests/test_holds.py` — State machine enforcement:
  - Attempt to capture a `VOIDED` hold → `409 hold_not_pending`.
  - Attempt to void a `CAPTURED` hold → `409 hold_not_pending`.
  - Attempt to capture an `EXPIRED` hold → `409 hold_not_pending`.
  - Verify the database trigger: attempt a raw SQL `UPDATE holds SET status='PENDING' WHERE status='CAPTURED'` as the app role and verify it raises an exception (terminal-state immutability).

- [x] 5.5 `tests/test_holds_idempotency.py` — Idempotency:
  - Send the same `authorize_hold` request twice with the same `Idempotency-Key` → second returns the replayed response with `Idempotent-Replayed: true` header, no duplicate hold.
  - Send the same `capture_hold` request twice → second replays, no duplicate journal entry.
  - Send the same `void_hold` request twice → second replays.
  - Same key with different payload → `422 idempotency_key_reuse`.

- [x] 5.6 `tests/test_holds_race.py` — Concurrency:
  - N threads authorize holds on the same account with limited balance. Verify that exactly the expected number succeed and the rest get `insufficient_funds`. Sum of successful hold amounts must equal the starting balance (for `allow_negative=False`).
  - Two threads attempt to capture the same hold concurrently. Verify exactly one succeeds and the other gets `hold_not_pending` (the loser sees the hold already `CAPTURED` after the winner's transaction commits).
  - Two threads attempt to void the same hold concurrently. Verify exactly one succeeds.

- [x] 5.7 `tests/test_holds_rls.py` — Cross-tenant isolation (extension of 1.3):
  - Tenant A creates a hold. Tenant B attempts to capture/void it → `404 not_found`.
  - Tenant A creates a hold. Tenant B retrieves `/holds/{id}/` → `404`.
  - Verify available-balance computation only includes the current tenant's holds (tenant A's holds do not affect tenant B's available balance for a shared-PK account, which is impossible with composite FKs but verify RLS prevents cross-reads in the aggregate query).

- [x] 5.8 `tests/test_holds_expiry.py` — Expiration worker:
  - Create a hold with `expires_at` in the past. Run `expire_holds` task. Verify `status='EXPIRED'` and an `OutboxEvent` with `type="hold.expired"` exists.
  - Create a hold with `expires_at` in the future. Run `expire_holds`. Verify `status` remains `PENDING`.
  - Create a `CAPTURED` hold with `expires_at` in the past. Run `expire_holds`. Verify it is untouched (the sweep only targets `PENDING` holds).
  - Run `expire_holds` twice on the same expired hold. Verify idempotency (no error, no duplicate outbox events).

- [x] 5.9 Run full test suite: `pytest tests/ -v` to confirm no regressions across posting, reconciliation, snapshots, webhooks, tenancy, and API tests. Verify the `AccountSerializer` output shape change is reflected in `test_api_endpoints.py`.
