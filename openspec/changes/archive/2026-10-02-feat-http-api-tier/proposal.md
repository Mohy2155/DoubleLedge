# Proposal

## Why

Doubleline currently enforces accounting correctness via PostgreSQL and provides domain logic in `ledger.services.posting`, but it lacks an external HTTP interface. We need to expose this functionality to external consumers (like B2B platforms, SaaS billing, and wallets) over an HTTP API that strictly enforces authentication, tenant isolation, and the ledger's invariants.

## What Changes

- Add a Django REST Framework (DRF) HTTP API tier handling incoming requests.
- Implement RS256 JWT authentication (requiring `exp` and valid `tenant_id` UUID) that extracts a `tenant_id` claim.
- Propagate the tenant context to PostgreSQL within the request transaction boundary via `SELECT set_config('app.tenant_id', %s, true)` for Row-Level Security (RLS).
- Add the following exact endpoints versioned under `/api/v1/`:
  - `POST /api/v1/journal-entries/`
  - `GET /api/v1/journal-entries/{id}/`
  - `POST /api/v1/journal-entries/{id}/reverse/` (accepts or derives a unique `reference_id` for the reversal entry)
  - `GET /api/v1/accounts/{id}/`
  - `GET /api/v1/accounts/{id}/balance/?as_of=<ts>` (uses timezone-aware `effective_at`)
  - `GET /api/v1/accounts/{id}/statement/`
- Enforce strict JSON string parsing for all monetary amounts (zero-float policy) using a custom JSON parser or DRF field.
- Map domain exceptions to standard HTTP error codes:
  - 400 for missing `Idempotency-Key` header
  - 401 for unauthenticated requests
  - 404 for cross-tenant RLS violations
  - 409 for conflicts (`already_reversed`)
  - 422 for idempotency key reuse or ledger errors
  - 429 for rate limit exceeded
  - 503 for lock timeout
- Use canonical JSON serialization (sorted keys, compact separators, UTF-8) combined with HTTP method and path for SHA-256 payload hashing to compute the idempotency request hash.
- Implement per-tenant Redis token bucket rate limiting.
- Provide OpenAPI schema generation via `drf-spectacular` and Swagger UI at `/api/docs/`.
- Set up structured JSON logging (deferring complex OTel collector setup and `POST /webhook-endpoints/`).

## Capabilities

### New Capabilities
- `api/http-gateway`: Defines the HTTP API endpoints, authentication (RS256 JWT), canonical payload hashing for idempotency, strict zero-float money validation, and error mapping to HTTP status codes.

### Modified Capabilities

## Impact

- **Code:** Adds new Django views, serializers, and custom DRF JSON parser/fields in the `api` app.
- **Dependencies:** Requires `djangorestframework`, a JWT library (`PyJWT[crypto]`), `redis`, and `drf-spectacular`.
- **APIs:** Exposes the new public-facing `/api/v1/` endpoints.
