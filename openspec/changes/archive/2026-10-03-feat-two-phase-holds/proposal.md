# Proposal

## Why

DoubleLedge currently supports only immediate, fully-settled postings. Real-world payment flows require a two-phase commit pattern — **authorize then capture** — where funds are reserved (held) before a final settlement decision. Without holds, platforms must either:

1. **Over-post then reverse** — post a full entry at authorization time and reverse it if the order is cancelled. This pollutes the ledger with reversal noise and makes statements unreadable.
2. **Track reservations externally** — maintain a separate "pending" table outside the ledger, creating a dual-write inconsistency risk that DoubleLedge was built to eliminate.

Both workarounds break the single-source-of-truth model. The `available_balance` (what a customer can actually spend) diverges from `accounts.balance` (what has been posted), and no single query can answer "how much can this account spend right now?" without joining across systems.

### Business cases

| Use case | Hold | Settlement |
| --- | --- | --- |
| Marketplace order | Reserve buyer wallet on checkout | Capture on shipment; void on cancellation |
| SaaS usage billing | Pre-authorize estimated usage | Capture actual usage at billing cycle end (partial capture) |
| Card-style authorization | Reserve funds at POS | Capture or release after settlement window |

## What Changes

A new `holds` table and supporting service layer that models the lifecycle:

```
PENDING  ──capture──►  CAPTURED   (creates a balanced JournalEntry)
   │
   ├───void────►  VOIDED     (no ledger impact; unreserves funds)
   │
   └───(expiry)──►  EXPIRED    (Celery sweep; unreserves funds)
```

### State lifecycle

| State | Meaning | Transition trigger |
| --- | --- | --- |
| `PENDING` | Funds reserved but not yet moved in the ledger | Initial authorization |
| `CAPTURED` | Hold settled; a balanced `JournalEntry` has been posted | `POST /holds/{id}/capture/` |
| `VOIDED` | Hold cancelled; reserved funds released, no ledger entry | `POST /holds/{id}/void/` |
| `EXPIRED` | Hold expired past `expires_at`; equivalent to void | Celery `expire_holds` sweep |

**Terminal states** are `CAPTURED`, `VOIDED`, and `EXPIRED`. Once terminal, a hold is immutable.

### Available balance

For any account:

```
available_balance = posted_balance − Σ(active holds against this account)
```

Where "active holds against this account" means holds in `PENDING` status where the account is the **source** (the side that would be debited for normal-side debit accounts, or credited for normal-side credit accounts — i.e., the side whose balance decreases). The polarity rules from the posting service apply: a hold on a `LIABILITY` account (e.g., customer wallet) reserves by decrementing `available_balance`, exactly as a debit would.

The `allow_negative` flag on the account governs whether `available_balance` may go below zero when authorizing a new hold.

## API Interface

All endpoints require `Authorization: Bearer <tenant_jwt>` and `Idempotency-Key` headers. All amounts are JSON strings (zero-float policy).

### `POST /api/v1/holds/`

Authorize a hold (reserve funds).

**Request:**
```json
{
  "reference_id": "ORDER_99283_HOLD",
  "account_id": "6f1c0a9e-...-user-wallet",
  "amount": "500.00",
  "currency": "USD",
  "description": "Hold for order 99283",
  "expires_at": "2026-10-10T00:00:00Z",
  "metadata": {}
}
```

**Response (201):**
```json
{
  "id": "b3a1c2d4-...",
  "reference_id": "ORDER_99283_HOLD",
  "account_id": "6f1c0a9e-...-user-wallet",
  "amount": "500.00",
  "currency": "USD",
  "status": "PENDING",
  "captured_amount": "0.0000",
  "description": "Hold for order 99283",
  "expires_at": "2026-10-10T00:00:00Z",
  "created_at": "2026-10-03T12:00:00Z",
  "metadata": {}
}
```

**Validation:**
- `currency` must match `account.currency`.
- `amount` must be a positive string decimal, validated to the account currency's minor-unit precision.
- `expires_at` must be in the future.
- `available_balance` after the hold must be ≥ 0 (unless `allow_negative` is true).
- `reference_id` must be unique per tenant (`UNIQUE (tenant_id, reference_id)`).

**Errors:**
| Status | `code` | Condition |
| --- | --- | --- |
| `400` | `missing_idempotency_key` | Header absent |
| `422` | `insufficient_funds` | Would breach available balance |
| `422` | `currency_mismatch` | Hold currency ≠ account currency |
| `422` | `invalid_expiry` | `expires_at` is in the past |
| `409` | `reference_conflict` | `reference_id` already used |
| `422` | `idempotency_key_reuse` | Same key, different payload |

### `POST /api/v1/holds/{id}/capture/`

Capture a pending hold, creating a balanced journal entry. Supports **partial captures**: capture an amount ≤ the hold amount. Any uncaptured remainder is automatically released (the hold transitions to `CAPTURED` with `captured_amount < amount`).

**Request:**
```json
{
  "amount": "450.00",
  "destination_account_id": "c2a7d3b1-...-merchant-pay",
  "reference_id": "ORDER_99283_CAPTURE",
  "description": "Capture for order 99283"
}
```

- `amount` is optional; defaults to the full hold amount minus any prior captured amount. Must be ≤ remaining uncaptured amount.
- `destination_account_id` is the credit side of the resulting journal entry (the hold's `account_id` is the debit side).
- `reference_id` is used as the journal entry's `reference_id`.

**Response (201):**
```json
{
  "hold_id": "b3a1c2d4-...",
  "status": "CAPTURED",
  "captured_amount": "450.00",
  "journal_entry_id": "e5f6a7b8-...",
  "released_amount": "50.00"
}
```

**Errors:**
| Status | `code` | Condition |
| --- | --- | --- |
| `404` | `not_found` | Hold not found or wrong tenant |
| `409` | `hold_not_pending` | Hold is not in `PENDING` status |
| `422` | `capture_exceeds_hold` | Capture amount > remaining hold amount |

### `POST /api/v1/holds/{id}/void/`

Void (release) a pending hold. No ledger entries are created; the reserved amount is simply freed.

**Request:** Empty body (or `{}`).

**Response (200):**
```json
{
  "hold_id": "b3a1c2d4-...",
  "status": "VOIDED",
  "released_amount": "500.00"
}
```

**Errors:**
| Status | `code` | Condition |
| --- | --- | --- |
| `404` | `not_found` | Hold not found or wrong tenant |
| `409` | `hold_not_pending` | Hold is not in `PENDING` status |

### `GET /api/v1/holds/{id}/`

Retrieve a hold by ID.

### `GET /api/v1/accounts/{id}/` (modified)

The existing account response is extended with:
```json
{
  "id": "...",
  "name": "...",
  "type": "LIABILITY",
  "posted_balance": "1000.00",
  "pending_holds": "500.00",
  "available_balance": "500.00",
  "version": 42
}
```

Where `posted_balance` is the existing `accounts.balance`, `pending_holds` is `SUM(amount - captured_amount)` of `PENDING` holds, and `available_balance = posted_balance − pending_holds` (with polarity adjustment for credit-normal accounts).

## Capabilities

### New Capabilities
- `ledger/holds`: Hold lifecycle management — authorization, capture, void, and expiration — with available-balance enforcement and atomic settlement via the posting engine.

### Modified Capabilities
- `api/http-gateway`: New hold endpoints and extended account serializer with available-balance fields.
- `ledger/posting`: No changes to the posting service itself; capture calls `post_entry` as a consumer.
- `ledger/reconciliation`: Extended to verify that `SUM(pending holds)` + `posted_balance` is consistent.

## Impact

- **Database**: New `holds` table with RLS, composite tenant FK, partial index on `PENDING` holds. Migration `0008_holds.py`.
- **Workers**: New Celery Beat task `expire_holds` using `for_each_tenant`.
- **API**: Three new endpoints, one modified serializer.
- **Dependencies**: None new.
- **Security**: RLS on `holds` table, same fail-closed policy. Cross-tenant access returns 404.
- **Deferred**: Multi-hold captures (capturing across multiple holds in one entry), hold amount increases, hold transfer between accounts.
