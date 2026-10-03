# Design

## Context

See proposal.md for motivation and API surface. DoubleLedge needs a holds layer that reserves funds without touching the ledger until capture, while maintaining all existing invariants: RLS tenant isolation, append-only ledger, atomic idempotency, and database-enforced correctness.

Already in place: the posting service (`post_entry`), idempotency layer (`get_or_insert_idempotency_key`), `for_each_tenant` worker pattern, `tenant_transaction` decorator, and the `OutboxEvent` transactional outbox. The holds feature is a new table and service that **consumes** `post_entry` at capture time rather than modifying it.

## Goals / Non-Goals

**Goals:**
- Model the full hold lifecycle (`PENDING → CAPTURED | VOIDED | EXPIRED`) as a first-class database entity with state-machine constraints.
- Compute `available_balance` atomically under the same row lock used by `post_entry`, so a hold authorization and a posting against the same account are mutually exclusive and both see a consistent view.
- Support partial captures where `captured_amount < hold_amount`, with the remainder automatically released.
- Enforce idempotency on all three mutating operations (authorize, capture, void) using the existing `IdempotencyKey` infrastructure.
- Expire stale holds via a Celery sweep without holding long-lived locks.
- Expose `posted_balance`, `pending_holds`, and `available_balance` on the account serializer.

**Non-Goals:**
- Multi-hold captures (settling multiple holds in a single journal entry). Deferred.
- Hold amount increases or extensions. A new hold can be created instead.
- Modifying `post_entry` itself. Capture creates a standard posting payload and calls `post_entry`.
- Reconciliation of holds against journal entries (deferred until hold volume justifies it).

## Decisions

### Decision 1: Holds table with status constraint and partial index

The `holds` table is a new mutable entity (unlike the append-only ledger tables). Status transitions are enforced by the application service with a `CHECK` constraint as a backstop. A partial index on `PENDING` status ensures that available-balance queries and the expiration sweep never scan terminal holds.

```sql
CREATE TABLE holds (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid        NOT NULL REFERENCES tenants(id),
    reference_id      text        NOT NULL,
    account_id        uuid        NOT NULL,
    currency          char(3)     NOT NULL,
    amount            numeric(19, 4) NOT NULL CHECK (amount > 0),
    captured_amount   numeric(19, 4) NOT NULL DEFAULT 0 CHECK (captured_amount >= 0),
    status            text        NOT NULL DEFAULT 'PENDING'
                      CHECK (status IN ('PENDING', 'CAPTURED', 'VOIDED', 'EXPIRED')),
    description       text        NOT NULL DEFAULT '',
    journal_entry_id  uuid,                       -- set on capture
    expires_at        timestamptz NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    metadata          jsonb       NOT NULL DEFAULT '{}',
    UNIQUE (tenant_id, reference_id),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, account_id)       REFERENCES accounts (tenant_id, id),
    FOREIGN KEY (tenant_id, journal_entry_id) REFERENCES journal_entries (tenant_id, id),
    CHECK (captured_amount <= amount)
);

-- Only pending holds matter for available_balance and expiry sweeps.
CREATE INDEX idx_holds_pending
    ON holds (tenant_id, account_id)
    WHERE status = 'PENDING';

-- Expiration sweep: find expired PENDING holds efficiently.
CREATE INDEX idx_holds_pending_expiry
    ON holds (expires_at)
    WHERE status = 'PENDING';
```

**Rationale for `captured_amount`:** A partial capture sets `captured_amount` to the captured portion and transitions to `CAPTURED`. The `released_amount` is `amount - captured_amount`, computed at read time. This avoids a separate `released_amount` column that could drift.

**Rationale against immutability triggers on `holds`:** Unlike `journal_entries` and `transaction_lines`, holds are inherently mutable (status transitions, `captured_amount` updates). The `CHECK` constraints prevent invalid states; application-level service logic enforces the state machine (only `PENDING → {CAPTURED, VOIDED, EXPIRED}`). An `updated_at` trigger keeps the timestamp current:

```sql
CREATE FUNCTION update_holds_timestamp() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_holds_updated_at
    BEFORE UPDATE ON holds
    FOR EACH ROW EXECUTE FUNCTION update_holds_timestamp();
```

**Terminal-state immutability trigger:** Once a hold reaches a terminal state, further updates must be blocked at the database level as a backstop:

```sql
CREATE FUNCTION block_terminal_hold_update() RETURNS trigger AS $$
BEGIN
    IF OLD.status IN ('CAPTURED', 'VOIDED', 'EXPIRED') THEN
        RAISE EXCEPTION 'Cannot modify hold % in terminal state %', OLD.id, OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_holds_terminal_immutable
    BEFORE UPDATE ON holds
    FOR EACH ROW EXECUTE FUNCTION block_terminal_hold_update();
```

The `updated_at` trigger fires after the terminal check, so both coexist. `DELETE` is not granted to the app role; holds are never deleted.

### Decision 2: Row-Level Security

Same fail-closed RLS policy as every other table. Zero static `GRANT` statements — the app role inherits access through the standard `SET LOCAL app.tenant_id` mechanism:

```sql
ALTER TABLE holds ENABLE ROW LEVEL SECURITY;
ALTER TABLE holds FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_holds ON holds
    USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
```

### Decision 3: Available-balance computation under account row lock

Authorization must atomically verify that the account can absorb the hold. This uses the same `SELECT FOR UPDATE` pattern as `post_entry`, acquiring the account row lock before reading the balance:

```python
@retry_on_deadlock(max_retries=3, base_delay=0.05)
@transaction.atomic
def authorize_hold(tenant, payload, idempotency_key, request_hash):
    # 1. Idempotency check (same pattern as post_entry)
    key_row, created = get_or_insert_idempotency_key(...)
    if not created: ...  # replay or reject

    # 2. Set tenant context + lock timeout
    cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.id)])
    cursor.execute("SET LOCAL lock_timeout = '2s'")

    # 3. Lock the account (deterministic order: single account)
    account = Account.objects.select_for_update().get(
        tenant_id=tenant.id, id=payload['account_id']
    )

    # 4. Validate currency match
    if account.currency != payload['currency']:
        raise HoldError("currency_mismatch")

    # 5. Compute available_balance
    #    SUM of PENDING holds' remaining amounts against this account
    pending_holds_total = Hold.objects.filter(
        tenant_id=tenant.id,
        account_id=account.id,
        status='PENDING'
    ).aggregate(
        total=Sum(F('amount') - F('captured_amount'))
    )['total'] or Decimal('0')

    available = compute_available_balance(account, pending_holds_total)
    hold_amount = Decimal(payload['amount'])

    if available - hold_amount < 0 and not account.allow_negative:
        raise HoldError("insufficient_funds")

    # 6. Insert hold
    hold = Hold.objects.create(
        tenant=tenant,
        reference_id=payload['reference_id'],
        account=account,
        currency=account.currency,
        amount=hold_amount,
        description=payload.get('description', ''),
        expires_at=payload['expires_at'],
        metadata=payload.get('metadata', {}),
    )

    # 7. Outbox event
    OutboxEvent.objects.create(tenant=tenant, type="hold.authorized", payload=...)

    # 8. Record idempotency
    ...
```

**Polarity-aware `compute_available_balance`:**

```python
def compute_available_balance(account, pending_holds_total):
    """
    available = posted_balance - pending_holds_total

    For debit-normal accounts (ASSET, EXPENSE):
        posted_balance is already in debit convention (positive = healthy).
        Holds reduce the available balance.

    For credit-normal accounts (LIABILITY, EQUITY, REVENUE):
        posted_balance is in credit convention (positive = healthy).
        Holds reduce the available balance.

    In both cases: available = balance - pending_holds_total.
    The posted_balance already has the correct sign from the posting engine.
    """
    return account.balance - pending_holds_total
```

The account row lock means no concurrent `post_entry` or `authorize_hold` can modify the balance or insert a hold between the read and the insert. This is the same mutual-exclusion mechanism the posting service uses.

### Decision 4: Settlement mechanics (capture)

Capture atomically:
1. Locks the hold row (`SELECT FOR UPDATE`).
2. Validates `status == 'PENDING'`.
3. Constructs a balanced posting payload (hold's account debited, destination account credited).
4. Calls `post_entry` — which handles its own account row locking, balance updates, idempotency, and outbox event.
5. Updates the hold: `status = 'CAPTURED'`, `captured_amount = capture_amount`, `journal_entry_id = entry.id`.

```python
@retry_on_deadlock(max_retries=3, base_delay=0.05)
@transaction.atomic
def capture_hold(tenant, hold_id, payload, idempotency_key, request_hash):
    # 1. Idempotency
    key_row, created = get_or_insert_idempotency_key(...)
    if not created: ...

    # 2. Tenant context
    cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.id)])
    cursor.execute("SET LOCAL lock_timeout = '2s'")

    # 3. Lock hold
    hold = Hold.objects.select_for_update().get(tenant_id=tenant.id, id=hold_id)

    if hold.status != 'PENDING':
        raise HoldError("hold_not_pending")

    # 4. Determine capture amount
    remaining = hold.amount - hold.captured_amount
    capture_amount = Decimal(payload.get('amount', str(remaining)))

    if capture_amount > remaining:
        raise HoldError("capture_exceeds_hold")
    if capture_amount <= 0:
        raise HoldError("invalid_amount")

    # 5. Build posting payload
    posting_payload = {
        'reference_id': payload['reference_id'],
        'description': payload.get('description', f'Capture of hold {hold.reference_id}'),
        'lines': [
            {
                'account_id': str(hold.account_id),
                'amount': str(capture_amount),
                'direction': _debit_direction_for_account(hold.account),
            },
            {
                'account_id': str(payload['destination_account_id']),
                'amount': str(capture_amount),
                'direction': _credit_direction_for_destination(hold.account),
            }
        ]
    }

    # 6. Post via existing engine (reuses its idempotency key separately)
    #    We use a derived idempotency key for the inner posting to avoid collision
    posting_idem_key = f"hold_capture:{hold.id}"
    posting_hash = generate_request_hash(posting_payload)
    code, body, _ = post_entry(tenant, posting_payload, posting_idem_key, posting_hash)

    # 7. Update hold
    hold.status = 'CAPTURED'
    hold.captured_amount = capture_amount
    hold.journal_entry_id = uuid.UUID(body['id'])
    hold.save(update_fields=['status', 'captured_amount', 'journal_entry_id', 'updated_at'])

    # 8. Outbox event for hold capture
    OutboxEvent.objects.create(tenant=tenant, type="hold.captured", payload=...)

    # 9. Record idempotency
    ...
```

**Direction helpers:** The debit/credit direction on the journal entry depends on the hold account's normal side. For a `LIABILITY` account (customer wallet), a capture debits the wallet (decreasing the liability) and credits the destination. The helpers mirror the polarity logic in `post_entry`.

**Partial capture:** If `capture_amount < hold.amount`, the hold still transitions to `CAPTURED`. The `released_amount = hold.amount - capture_amount` is computed at response time. There is no separate "partially captured" state — a hold is either `PENDING` or settled. This avoids state proliferation and matches industry convention (Stripe, Adyen).

**Lock ordering with `post_entry`:** The capture function locks the hold row first, then `post_entry` locks the account rows in ascending PK order. Since `authorize_hold` locks the account (not the hold row), and `capture_hold` locks the hold row then delegates to `post_entry` (which locks accounts), there is no cycle. Two concurrent captures on different holds touching the same accounts are serialized by `post_entry`'s account locks, not the hold locks.

### Decision 5: Void mechanics

Void is simpler — no journal entry is created:

```python
@retry_on_deadlock(max_retries=3, base_delay=0.05)
@transaction.atomic
def void_hold(tenant, hold_id, idempotency_key, request_hash):
    # Idempotency, tenant context, lock timeout (same pattern)
    ...
    hold = Hold.objects.select_for_update().get(tenant_id=tenant.id, id=hold_id)

    if hold.status != 'PENDING':
        raise HoldError("hold_not_pending")

    hold.status = 'VOIDED'
    hold.save(update_fields=['status', 'updated_at'])

    OutboxEvent.objects.create(tenant=tenant, type="hold.voided", payload=...)
    # Record idempotency
    ...
```

No account lock is needed because voiding does not modify `accounts.balance`. The hold's `PENDING → VOIDED` transition alone frees the reserved amount in subsequent `available_balance` computations (the `PENDING` partial index no longer includes the row).

### Decision 6: Expiration worker

A Celery Beat task sweeps expired `PENDING` holds using the existing `for_each_tenant` pattern:

```python
@shared_task(ignore_result=True)
def expire_holds():
    def run_for_tenant(tenant_id):
        # Batch-expire holds past their expiry time.
        # Use FOR UPDATE SKIP LOCKED so concurrent workers don't fight.
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE holds
                SET status = 'EXPIRED', updated_at = now()
                WHERE id IN (
                    SELECT id FROM holds
                    WHERE status = 'PENDING'
                      AND expires_at <= now()
                    ORDER BY expires_at
                    LIMIT 500
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, tenant_id, account_id, amount, reference_id
            """)
            expired_rows = cursor.fetchall()

        # Emit outbox events for each expired hold
        for row in expired_rows:
            OutboxEvent.objects.create(
                tenant_id=row[1],
                type="hold.expired",
                payload={
                    "hold_id": str(row[0]),
                    "account_id": str(row[2]),
                    "amount": str(row[3]),
                    "reference_id": row[4],
                }
            )

    for_each_tenant(run_for_tenant)
```

**Why no account lock on expiry:** Expiration only changes `holds.status`, not `accounts.balance`. The available-balance computation dynamically sums `PENDING` holds, so expiring a hold immediately increases `available_balance` on the next read without touching the account row. This keeps the sweep fast and contention-free.

**Batch size:** Limited to 500 per tenant per tick to bound transaction duration. The partial index `idx_holds_pending_expiry` makes the scan efficient.

**Idempotency:** The `UPDATE ... WHERE status = 'PENDING'` is naturally idempotent — an already-expired hold won't match the `WHERE` clause. The terminal-state trigger provides a further backstop.

### Decision 7: Django model (managed = False)

Following the established pattern (every ledger model uses `managed = False` with DDL in raw SQL migrations):

```python
class Hold(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING'
        CAPTURED = 'CAPTURED'
        VOIDED = 'VOIDED'
        EXPIRED = 'EXPIRED'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    reference_id = models.TextField()
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='holds')
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=19, decimal_places=4)
    captured_amount = models.DecimalField(max_digits=19, decimal_places=4, default=0)
    status = models.TextField(choices=Status.choices, default=Status.PENDING)
    description = models.TextField(default='')
    journal_entry = models.ForeignKey(
        JournalEntry, null=True, on_delete=models.PROTECT, related_name='hold'
    )
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    metadata = models.JSONField(default=dict)

    class Meta:
        managed = False
        db_table = 'holds'
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'id'], name='unique_tenant_hold'),
            models.UniqueConstraint(fields=['tenant', 'reference_id'], name='unique_tenant_hold_reference'),
        ]
```

### Decision 8: Serializer updates

**`AccountSerializer` extension:**

The existing `AccountSerializer` is extended to include hold-aware balance fields:

```python
class AccountSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    type = serializers.CharField(read_only=True)
    currency = serializers.CharField(read_only=True)
    posted_balance = StrictStringDecimalField(max_digits=19, decimal_places=4, read_only=True)
    pending_holds = StrictStringDecimalField(max_digits=19, decimal_places=4, read_only=True)
    available_balance = StrictStringDecimalField(max_digits=19, decimal_places=4, read_only=True)
    version = serializers.IntegerField(read_only=True)
```

The view annotates the account before serialization:

```python
def retrieve(self, request, pk=None):
    account = get_object_or_404(Account, id=pk, tenant_id=tenant_id)
    pending_total = Hold.objects.filter(
        tenant_id=tenant_id, account_id=account.id, status='PENDING'
    ).aggregate(total=Sum(F('amount') - F('captured_amount')))['total'] or Decimal('0')

    account.posted_balance = account.balance
    account.pending_holds = pending_total
    account.available_balance = account.balance - pending_total
    ...
```

The field previously named `balance` becomes `posted_balance` in the serializer output. This is a **breaking change** to the account endpoint response shape. The `AccountBalanceSerializer` (for the `?as_of=` temporal query) is unchanged — temporal hold queries are not in scope.

**New serializers:**

```python
class HoldSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    reference_id = serializers.CharField(read_only=True)
    account_id = serializers.UUIDField(read_only=True)
    amount = StrictStringDecimalField(max_digits=19, decimal_places=4, read_only=True)
    captured_amount = StrictStringDecimalField(max_digits=19, decimal_places=4, read_only=True)
    currency = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    journal_entry_id = serializers.UUIDField(read_only=True, allow_null=True)
    expires_at = serializers.DateTimeField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    metadata = serializers.JSONField(read_only=True)

class HoldCreateSerializer(serializers.Serializer):
    reference_id = serializers.CharField(max_length=255)
    account_id = serializers.UUIDField()
    amount = StrictStringDecimalField(max_digits=19, decimal_places=4)
    currency = serializers.CharField(max_length=3)
    description = serializers.CharField(max_length=255, required=False, default='')
    expires_at = serializers.DateTimeField()
    metadata = serializers.JSONField(required=False, default=dict)

class HoldCaptureSerializer(serializers.Serializer):
    amount = StrictStringDecimalField(max_digits=19, decimal_places=4, required=False)
    destination_account_id = serializers.UUIDField()
    reference_id = serializers.CharField(max_length=255)
    description = serializers.CharField(max_length=255, required=False, default='')
```

### Decision 9: Outbox event types

Three new event types for webhook subscribers:

| Event type | Emitted on |
| --- | --- |
| `hold.authorized` | Successful hold creation |
| `hold.captured` | Successful capture (the existing `entry.posted` event also fires for the journal entry) |
| `hold.voided` | Successful void |
| `hold.expired` | Expiration by the sweep worker |

Webhook endpoints that subscribe to these types must be added to the `event_types` choices in `WebhookEndpointSerializer`. Existing endpoints subscribing only to `entry.*` are unaffected.

### Decision 10: Migration structure (`0008_holds.py`)

A single `RunSQL` migration following the pattern established in `0001_initial.py`:

1. `CREATE TABLE holds` with all columns, constraints, and composite FKs.
2. Partial indexes (`idx_holds_pending`, `idx_holds_pending_expiry`).
3. `updated_at` trigger function and trigger.
4. Terminal-state immutability trigger function and trigger.
5. `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` + `CREATE POLICY`.
6. **No `GRANT` statements** — consistent with the zero-static-GRANT convention.
7. Reverse SQL: `DROP TABLE IF EXISTS holds CASCADE; DROP FUNCTION IF EXISTS ...`.

## Risks / Trade-offs

- **[Risk] Hold-account lock contention with postings.** Authorization locks the account row (`FOR UPDATE`), which serializes against concurrent `post_entry` calls on the same account. → **Mitigation:** Same bounded `lock_timeout = '2s'` and deadlock retry as postings. Hold authorization is a fast operation (no journal entry, no line inserts), so the lock is held briefly.
- **[Risk] Stale holds inflating reserved amounts.** A hold with a far-future `expires_at` and no capture or void would permanently reduce `available_balance`. → **Mitigation:** The expiration sweep is the backstop; API callers should set reasonable expiry windows. A future enhancement could add maximum hold duration as a tenant-level setting.
- **[Risk] Breaking change to `AccountSerializer`.** Renaming `balance` to `posted_balance` and adding new fields changes the API contract. → **Mitigation:** The API is not yet versioned or released to external consumers. Document the change clearly. If backward compatibility is needed later, version the serializer.
- **[Risk] Capture calling `post_entry` with a derived idempotency key.** If the outer transaction rolls back after `post_entry` commits (impossible under `@transaction.atomic` since `post_entry` runs in the same transaction), the journal entry would be orphaned. → **Mitigation:** `post_entry` is decorated with `@transaction.atomic`, and capture calls it within its own `@transaction.atomic` — they share the same outermost savepoint. If the capture fails after `post_entry` returns, the entire transaction rolls back, including the journal entry, lines, and outbox event. No orphan is possible.
- **[Trade-off] No separate `PARTIALLY_CAPTURED` status.** A partial capture transitions directly to `CAPTURED`. This simplifies the state machine but means the unreleased remainder is implicit (`amount - captured_amount`). This matches Stripe/Adyen convention and avoids multi-capture complexity.
