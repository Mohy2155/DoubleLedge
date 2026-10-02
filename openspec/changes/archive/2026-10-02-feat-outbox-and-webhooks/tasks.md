# Tasks

## 1. Schema and Models

- [x] 1.1 Verify existing state: confirm `outbox_events` has RLS from `0001_initial`, the `OutboxEvent` model exists, `post_entry` already writes the outbox row, and the app role can `SELECT` from `tenants`. If it cannot, decide on the dedicated worker-role fallback before continuing. Verify with a test running as the restricted app role (not the Compose superuser).
- [x] 1.2 Create a raw SQL migration adding `webhook_endpoints` and `webhook_deliveries`, adding `outbox_events.fanned_out_at`, the two partial indexes, composite tenant FKs, `ENABLE` + `FORCE ROW LEVEL SECURITY` policies on every new table, and explicit grants (`SELECT/INSERT/UPDATE`, plus `DELETE` where pruning needs it). Verify `manage.py migrate` runs cleanly and that cross-tenant reads/writes on the new tables fail as the app role.
- [x] 1.3 Add `WebhookEndpoint` and `WebhookDelivery` models and the `fanned_out_at` field on `OutboxEvent`, following the convention used by the existing ledger models (no duplicate `CREATE TABLE` migration). Verify via `makemigrations --dry-run` and a model import in the Django shell.

## 2. Webhook Configuration API

- [x] 2.1 Implement `/api/v1/webhook-endpoints/` (create, list, retrieve, update, delete) with serializers: `https`-only URL validation (with registration-time SSRF check), `event_types` limited to `entry.posted` / `entry.reversed`, and server-generated `whsec_` secret returned only on creation. Exempt these endpoints from the `Idempotency-Key` layer; duplicate `(tenant, url)` returns 409. Verify unit tests for secret one-time visibility (absent from GET/list), duplicate 409, and validation errors.
- [x] 2.2 Wire the URLs and verify a 404 when fetching, updating, or deleting another tenant's endpoint, and that deleting an endpoint removes its delivery rows.

## 3. Posting Engine Outbox Integration

- [x] 3.1 Update `ledger.services.posting.post_entry` so the outbox event type is `entry.reversed` when `reverses_id` is set and `entry.posted` otherwise, and the payload matches the design (all amounts as strings, `reverses_id` included). Verify `OutboxEvent` count increments in unit tests and that the payload contains no float or `Decimal` values.
- [x] 3.2 Add a test that intentionally fails journal entry posting and verify the outbox event is rolled back completely.
- [x] 3.3 Add a test that reversing an entry emits exactly one `entry.reversed` event, and that an idempotent replay emits no additional event.

## 4. Delivery Worker

- [x] 4.1 Add (or reuse, if the reconciliation change already created it) a `for_each_tenant` helper that opens a transaction per tenant and runs `SELECT set_config('app.tenant_id', %s, true)`. Verify with a test that the relay run with no tenant context fails loudly instead of reporting success.
- [x] 4.2 Implement the fan-out step: claim un-fanned-out events with `FOR UPDATE SKIP LOCKED`, insert one `webhook_deliveries` row per active endpoint subscribed to the event type (`ON CONFLICT DO NOTHING`), and set `fanned_out_at` in the same transaction. Verify: correct rows per endpoint, event-type filtering, inactive endpoints skipped, zero-endpoint events still marked fanned out, and a mid-step crash leaves the event un-fanned-out.
- [x] 4.3 Implement the claim step with a lease (`UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED LIMIT n) ... RETURNING`), including the sweep that marks exhausted `PENDING` deliveries `FAILED`. Verify with a concurrency test that two workers never claim the same delivery, and that a delivery whose lease expired (simulated crash) is redelivered.
- [x] 4.4 Implement the SSRF-safe sender and HMAC signing: add `requests` (or `httpx`) to `pyproject.toml`; resolve and validate IPs at send time and connect to the validated IP; https only (dev setting overrides); no redirects; response-size cap; 5s timeout; headers `X-Webhook-Id`, `X-Webhook-Timestamp`, `X-Webhook-Signature: v1=...` over `timestamp + "." + raw body`. Verify the signature against a fixed test vector and add one test per blocked address range (loopback, private, link-local/metadata, reserved) plus a redirect-refusal test.
- [x] 4.5 Implement result recording with the attempts fence: 2xx marks `DELIVERED`; anything else records `last_error` / `last_status_code`, sets `next_attempt_at` using exponential backoff with jitter, and marks `FAILED` at `max_attempts`. Verify mocked unit tests for success, retry scheduling, terminal failure, stale-result rejection (fence), and that one failing endpoint does not block or cause resends to another endpoint of the same event.
- [x] 4.6 Create the `workers.tasks.dispatch_webhooks` Celery task wiring fan-out, claim, send, and record per tenant, and configure Celery Beat to run it periodically. Verify the worker picks up the schedule and an end-to-end test delivers a posted entry to a local mock receiver.
- [x] 4.7 Implement the pruning Beat task (per tenant): delete terminal deliveries older than the retention window, then fully fanned-out events older than the window with no remaining deliveries. Verify with a test that recent, pending, and still-referenced rows are retained.
