# Design

## Context

The DoubleLedge service is currently enforced entirely at the database layer and by the domain logic in `ledger.services.posting`. To integrate with client applications, an HTTP API gateway must be introduced. See proposal.md for motivation - don't restate it.

## Goals / Non-Goals

**Goals:**
- Provide a standard RESTful HTTP API utilizing Django Rest Framework.
- Ensure all endpoints are correctly versioned under `/api/v1/`.
- Guarantee multi-tenant safety by bridging the stateless API layer (JWT `tenant_id`) with PostgreSQL Row-Level Security (RLS) via `SELECT set_config('app.tenant_id', %s, true)`.
- Safely handle monetary values via strict zero-float validation rules.
- Guarantee reliable idempotency checks by hashing the method, path, and canonical payload.
- Provide per-tenant token bucket rate limiting to prevent abuse.

**Non-Goals:**
- Complex OpenTelemetry configuration or deployment of an OTel collector (structured JSON logging is sufficient for now).
- Implementation of the `POST /webhook-endpoints/` API (deferred).
- Any modifications to the database schema or the existing `ledger.services.posting` core logic.

## Decisions

- **Authentication via DRF Class & RLS Context Propagation:** 
  - *Rationale*: Django middleware runs before DRF authentication. A custom DRF authentication class (using `PyJWT[crypto]` with `algorithms=["RS256"]`) is cleaner. Setting the tenant context will happen in a view-level `transaction.atomic()` wrapper using `SELECT set_config('app.tenant_id', %s, true)` to properly bind parameters and enable deadlock retries for the entire transaction block.
- **Zero-Float Money Validation:**
  - *Rationale*: DRF's default JSON parser converts JSON numbers into floats before serializers see them. To prevent this, we will use a custom JSON parser (with `parse_float` hooks that raise an error) or a custom DRF serializer field that strictly checks `isinstance(value, str)`.
- **Idempotency Hash (Method + Path + Body):**
  - *Rationale*: Hashing only the body means empty-body requests (like reversals) collide. The request body will be canonically serialized (UTF-8, sorted keys, compact separators) and concatenated with the HTTP method and path before applying SHA-256. 
- **Redis Token Bucket Rate Limiting:**
  - *Rationale*: DRF's default throttles are fixed-window. We need a custom throttle class keyed on `tenant_id` that implements a token bucket algorithm via a Redis Lua script. It will fail open if Redis is unreachable.
- **Exception Mapping via Custom DRF Handler:**
  - *Rationale*: Domain exceptions must map to RFC-compliant HTTP codes (400, 401, 404, 409, 422, 429, 503) globally to prevent manual `try/except` boilerplate in every view.

## Risks / Trade-offs

- **Risk:** Failure to wrap the view in an atomic transaction could break RLS isolation or idempotency guarantees.
  - *Mitigation:* Ensure a global view mixin or decorator strictly enforces the transaction and `set_config` logic before any view logic executes. Ensure connection is a non-superuser to respect RLS.
- **Risk:** Rate limiting in Redis fails when Redis is unavailable.
  - *Mitigation:* Design the custom token-bucket rate-limiter to fail open or degrade gracefully if Redis is unreachable.
