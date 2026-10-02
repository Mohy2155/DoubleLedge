# Proposal

## Why

DoubleLedge needs a reliable way to notify external systems when journal entries are posted or reversed without risking dual-write inconsistencies. By using a Transactional Outbox pattern, we guarantee that if a ledger transaction commits, the event will eventually be delivered via webhooks, and if it rolls back, the event is never emitted.

The `outbox_events` table (with RLS) and the `OutboxEvent` model already exist from the scaffold and posting changes, and `post_entry` already writes to it. This change builds the delivery side: webhook configuration, per-endpoint delivery tracking, and the relay worker.

## What Changes

- Add `webhook_endpoints` and `webhook_deliveries` tables (new raw SQL migration) with `FORCE ROW LEVEL SECURITY`, composite tenant foreign keys, and explicit grants. Add a `fanned_out_at` column and a partial index to the existing `outbox_events` table.
- Introduce `/api/v1/webhook-endpoints/` (create, list, retrieve, update, delete) so tenants can configure destinations. Each endpoint gets a generated HMAC-SHA256 secret that is returned exactly once, at creation. Endpoints subscribe to event types (`entry.posted`, `entry.reversed`).
- Update `post_entry` (verify existing behavior first): one outbox event per committed entry, typed `entry.reversed` when `reverses_id` is set and `entry.posted` otherwise, with all amounts serialized as strings in the `jsonb` payload.
- Add a Celery relay that, per tenant, (1) fans each new outbox event out into one `webhook_deliveries` row per matching active endpoint, (2) claims due deliveries with `SELECT ... FOR UPDATE SKIP LOCKED` under a short lease, (3) sends them outside any DB transaction, and (4) records the result with exponential backoff and a terminal `FAILED` state after a maximum number of attempts.
- Sign every delivery with HMAC-SHA256 over `timestamp + "." + body`, sent with the event ID and timestamp in headers.
- Harden outbound requests against SSRF (https only, connect-time IP validation, no redirects, response-size cap, 5s timeout).
- Add a Celery Beat pruning task for old terminal deliveries and fully-fanned-out events.

## Capabilities

### New Capabilities
- `ledger/outbox`: Per-endpoint delivery tracking and the Celery relay that fans out outbox events and reliably delivers them to external webhooks (at-least-once).
- `api/webhooks`: The configuration endpoints allowing tenants to manage webhook destinations and event-type subscriptions.

### Modified Capabilities
- `ledger/posting`: Event type selection (`entry.posted` vs `entry.reversed`) and string-serialized money in the outbox payload.

## Impact

- **Database**: New `webhook_endpoints` and `webhook_deliveries` tables; new `fanned_out_at` column and index on `outbox_events`. The existing `delivered_at` and `attempts` columns on `outbox_events` are superseded by `webhook_deliveries` and left unused.
- **Workers**: New Celery tasks for fan-out/delivery and pruning, running per tenant under RLS (shared tenant-iteration helper).
- **Dependencies**: `requests` (or `httpx`) for outbound delivery.
- **Security**: RLS on all new tables, SSRF protections on tenant-supplied URLs, HMAC-signed deliveries with replay protection via timestamp.
- **Deferred**: Circuit breaking / automatic endpoint disabling, secret rotation, replay of past events to new endpoints.
