# Design

## Context

See proposal.md for motivation. The ledger must safely emit reliable events to HTTP webhooks upon transaction commits without suffering from dual-write inconsistencies (where a database commit succeeds but the network HTTP call fails, or vice versa).

Already in place from earlier changes: the `outbox_events` table (with RLS), the `OutboxEvent` model, and an outbox write inside `post_entry`. This change adds the delivery side.

## Goals / Non-Goals

**Goals:**
- Guarantee at-least-once delivery for webhook events corresponding to successful database transactions.
- Keep the `post_entry` synchronous API fast by moving all network I/O and fan-out to background workers.
- Safely parallelize dispatch using multiple Celery workers without duplicate claims.
- Track delivery state per endpoint so one failing receiver never causes resends to, or blocks, other receivers.
- Protect the worker network from tenant-supplied URLs (SSRF).

**Non-Goals:**
- Exactly-once delivery. Receivers must deduplicate on the event ID (`X-Webhook-Id` header / `id` in the body).
- In-order delivery across events (backoff means a failed event may be retried after a later one succeeded).
- Circuit breaking / auto-disabling endpoints, secret rotation, and replaying historical events to newly created endpoints (all deferred).

## Decisions

**1. Transactional Outbox Pattern over Dual-Writes**
- **Rationale**: Direct HTTP calls or pushing to Redis inside `post_entry` risk inconsistency if the DB transaction rolls back after the call, or the call fails after the commit.
- **Alternatives Considered**: CDC via Debezium/WAL. Rejected to avoid Kafka/Debezium operational overhead; a polled table is sufficient at current scale.

**2. Per-endpoint delivery rows (`webhook_deliveries`)**
- **Rationale**: A single `delivered_at` on `outbox_events` cannot represent an event going to several endpoints. If endpoint A succeeds and B fails, a retry would resend to A. The relay therefore fans each event out into one delivery row per matching endpoint. `outbox_events.delivered_at` and `attempts` are superseded and left unused (no destructive schema change).
- **Fan-out**: In a tenant transaction, select `outbox_events WHERE fanned_out_at IS NULL ... FOR UPDATE SKIP LOCKED LIMIT n`, insert deliveries for each active endpoint whose `event_types` contains the event type (`ON CONFLICT (event_id, endpoint_id) DO NOTHING`), then set `fanned_out_at`. This is atomic, so a crash leaves the event un-fanned-out and it is retried. Events with no matching endpoint are still marked fanned out. Endpoints created later do not receive past events.

**3. Claim with a lease; never hold row locks during HTTP**
- **Rationale**: Holding `FOR UPDATE` plus a DB connection across network calls (batch size × up to 5s) starves the pool and blocks other workers. Instead:
  1. *Claim* (short transaction): `UPDATE webhook_deliveries SET attempts = attempts + 1, next_attempt_at = now() + lease WHERE id IN (SELECT id FROM webhook_deliveries WHERE status = 'PENDING' AND next_attempt_at <= now() AND attempts < max_attempts ORDER BY next_attempt_at LIMIT n FOR UPDATE SKIP LOCKED) RETURNING ...`, then commit.
  2. *Send* with no open transaction.
  3. *Record result* (short transaction) with a fence: `WHERE id = %s AND attempts = %s`, so a slow worker that outlived its lease cannot overwrite a newer attempt.
- A crashed worker's rows become claimable again when the lease expires, which preserves at-least-once. Defaults: batch size 10, request timeout 5s, lease 120s (must exceed worst-case batch time). Deliveries stuck `PENDING` with `attempts >= max_attempts` (crash on the last attempt) are marked `FAILED` by a sweep at the start of each claim step.
- **Retry policy**: any non-2xx or network error is retried with exponential backoff plus jitter (base 30s, cap 1h), up to `max_attempts` (default 8), then `FAILED` with `last_error` retained. All values are settings.

**4. RLS and worker tenant context**
- **Problem**: Celery tasks carry no `app.tenant_id`. With `FORCE ROW LEVEL SECURITY` and a non-owner app role, queries return zero rows, so events would silently pile up.
- **Decision**: Workers iterate over tenants. For each tenant they open a transaction, run `SELECT set_config('app.tenant_id', %s, true)`, and perform fan-out, claim, and result-recording inside that tenant's context. This keeps the "no `BYPASSRLS`" invariant. A shared helper (e.g. `ledger/services/tenancy.py: for_each_tenant`) is used here and by the reconciliation job; create it in whichever change lands first.
- **Prerequisite**: the app role must be able to `SELECT` from `tenants`. Verify this first. If it cannot, the fallback is a dedicated worker role whose policies are limited to `outbox_events`, `webhook_endpoints`, and `webhook_deliveries` (still narrower than `BYPASSRLS`).
- **Trade-off**: per-tenant iteration costs one cheap transaction per tenant per tick, even for idle tenants. Acceptable now; revisit with a pending-work hint table if tenant count grows.

**5. SSRF protection for tenant-supplied URLs**
- Tenants supply arbitrary URLs, and workers run inside the Compose/VPC network where `db`, `redis`, and cloud metadata (`169.254.169.254`) are reachable.
- Required controls:
  - `https` only. A dev-only setting (`WEBHOOK_ALLOW_INSECURE_URLS`) permits `http` and private addresses for local receivers.
  - Resolve the hostname at send time, reject if *any* resolved address is private, loopback, link-local, reserved, multicast, or unspecified (use `ipaddress`), and connect to the validated IP while preserving the Host header and TLS server name. Hostname-only checks lose to DNS rebinding. Registration-time validation is a convenience, not the boundary.
  - `allow_redirects=False`.
  - Cap the response body read (e.g. 64 KB); the body is only needed for diagnostics.
  - Strict connect/read timeouts (5s).

**6. Signing and payload format**
- Body: `{"id": <event uuid>, "type": "entry.posted" | "entry.reversed", "created_at": ..., "data": {...}}`. `data` holds entry id, `reference_id`, `description`, `effective_at`, `posted_at`, `reverses_id` (nullable), and `lines[]` with `account_id`, `direction`, and `amount`. **All amounts are strings** (`Decimal` is not JSON-serializable and floats violate the zero-float policy).
- Headers: `X-Webhook-Id` (event id), `X-Webhook-Timestamp` (unix seconds), `X-Webhook-Signature: v1=<hex>` where the signature is `HMAC_SHA256(secret, f"{timestamp}.{raw_body}")` over the exact bytes sent. Receivers should reject timestamps older than ~5 minutes and dedupe on the event id.
- The secret must be recoverable to sign, so it is stored as-is (not hashed), shown once at creation, never returned by GET/list, and never logged. (Encryption at rest is a roadmap item.)
- `Idempotency-Key` is the client's *inbound* header and plays no role in outgoing webhooks.

**7. Webhook configuration API**
- Endpoints: `POST`, `GET` (list), `GET {id}`, `PATCH`, `DELETE` under `/api/v1/webhook-endpoints/`. Inherits auth, tenant context, rate limiting, and error mapping from the HTTP gateway. Cross-tenant access returns 404 via RLS.
- Fields: `url`, `event_types` (subset of the supported types, default all), `is_active`, and a server-generated `secret` (`whsec_` + `secrets.token_urlsafe(32)`).
- **Idempotency exemption**: these endpoints do not use the `Idempotency-Key` layer, because the replay cache would persist the one-time secret in `idempotency_keys.response_body`. Duplicate creation is prevented by `UNIQUE (tenant_id, url)`, returning `409`. This is an intentional exception to README's "every mutating request requires an `Idempotency-Key`"; update README accordingly.
- `DELETE` removes the endpoint; its delivery rows are removed via `ON DELETE CASCADE`.

**8. Data model (new raw SQL migration; mirror README conventions)**
- `webhook_endpoints`: `id uuid PK`, `tenant_id`, `url`, `secret`, `event_types text[]`, `is_active`, `created_at`, `UNIQUE (tenant_id, id)`, `UNIQUE (tenant_id, url)`.
- `webhook_deliveries`: `id uuid PK`, `tenant_id`, `event_id`, `endpoint_id`, `status` (`PENDING`/`DELIVERED`/`FAILED`), `attempts`, `next_attempt_at`, `last_error`, `last_status_code`, `delivered_at`, `created_at`, `UNIQUE (event_id, endpoint_id)`, composite FKs on `(tenant_id, event_id)` and `(tenant_id, endpoint_id)` (`ON DELETE CASCADE` for endpoints).
- `outbox_events`: add `fanned_out_at timestamptz`.
- Indexes: partial index on `outbox_events (created_at) WHERE fanned_out_at IS NULL`, and on `webhook_deliveries (next_attempt_at) WHERE status = 'PENDING'`, so polling never scans history.
- Every new table gets `ENABLE` + `FORCE ROW LEVEL SECURITY` with the standard fail-closed policy, and explicit grants to the app role: `SELECT/INSERT/UPDATE` on deliveries and endpoints, `UPDATE` on `outbox_events.fanned_out_at`, and `DELETE` where pruning needs it. The ledger immutability grants do not cover these tables, so grant deliberately rather than by default.
- Model declaration follows whatever convention the existing ledger models use (do not generate a second `CREATE TABLE` migration for tables created by `RunSQL`).

**9. Pruning**
- A Beat task (per tenant) deletes terminal (`DELIVERED`/`FAILED`) deliveries older than 30 days, then fully fanned-out outbox events older than 30 days with no remaining deliveries. Retention is a setting.

## Risks / Trade-offs

- [Risk] Table bloat from outbox and delivery history. → Mitigation: pruning task and partial indexes (decisions 8 and 9).
- [Risk] Webhook storms from slow or misconfigured receivers. → Mitigation: 5s timeouts, bounded batch size, per-delivery backoff, max attempts, and per-endpoint rows so one bad receiver cannot block others. Circuit breaking is deferred.
- [Risk] Duplicate deliveries after a lease expires mid-send. → Accepted: at-least-once by design; the attempts fence prevents stale result writes and receivers dedupe on event id.
- [Risk] Worker silently sees no rows because RLS has no tenant context. → Mitigation: per-tenant iteration (decision 4) plus a test that runs the relay with no tenant context and asserts it fails loudly rather than reporting success.
- [Risk] SSRF through tenant URLs. → Mitigation: decision 5, with a test per blocked address range.
