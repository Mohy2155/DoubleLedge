# api/http-gateway Specification

## Purpose
Exposes the DoubleLedge ledger service to external clients via a secure HTTP API, enforcing authentication, isolation, and idempotency.

## Requirements

### Requirement: Authentication and Tenant Context
The system SHALL authenticate incoming requests using an RS256 JWT (with `exp` and `tenant_id` UUID validation). The `tenant_id` claim MUST be propagated to PostgreSQL using `SELECT set_config('app.tenant_id', %s, true)` within the request's transaction boundary.

#### Scenario: Request with valid JWT
- **WHEN** an HTTP request includes a valid RS256 JWT in the Authorization header
- **THEN** the API extracts the `tenant_id` and sets the database transaction context before querying

#### Scenario: Unauthenticated request
- **WHEN** an HTTP request lacks a valid token or the token is expired/invalid
- **THEN** the API fails the request closed with a 401 Unauthorized

### Requirement: Strict Input Validation
The system SHALL reject monetary amounts that are not represented as JSON strings to enforce a zero-float policy.

#### Scenario: Numeric float in request
- **WHEN** the request payload contains a monetary amount as a JSON number
- **THEN** the parser or serializer rejects the request with a 422 Unprocessable Entity

### Requirement: Idempotent Request Hashing
The system SHALL compute idempotency request hashes using a SHA-256 hash of the HTTP method, the request path, and the canonical JSON representation (UTF-8, sorted keys, compact separators) of the body.

#### Scenario: Reversals of different entries
- **WHEN** a client sends two empty-body POST requests to different endpoints (e.g. `/journal-entries/A/reverse/` and `/B/reverse/`)
- **THEN** the idempotency hashes differ because the paths are included in the hash computation

### Requirement: Idempotency Headers
The system SHALL require an `Idempotency-Key` header for state-changing endpoints and handle key reuse appropriately.

#### Scenario: Missing header
- **WHEN** a state-changing POST request lacks the `Idempotency-Key` header
- **THEN** the API returns a 400 Bad Request

#### Scenario: Key reused with different payload
- **WHEN** a request uses an existing `Idempotency-Key` but the computed hash differs from the stored hash
- **THEN** the API returns a 422 Unprocessable Entity

### Requirement: Error Mapping
The system SHALL map core domain exceptions and access control issues to standard HTTP status codes. When a request's transaction fails with a deadlock, serialization failure or lock timeout, the API SHALL retry the whole transaction up to 3 times before returning 503.

#### Scenario: Lock timeout occurs
- **WHEN** every attempt of a request's transaction fails with a deadlock, serialization failure or lock timeout
- **THEN** the API returns a 503 Service Unavailable with error code `lock_timeout` and a `Retry-After: 1` header

#### Scenario: Transient failure recovered
- **WHEN** the first attempt of a request's transaction fails with a deadlock and a retry succeeds
- **THEN** the client receives the normal response, and a mutating request's effects are applied exactly once

#### Scenario: Cross-tenant access
- **WHEN** a request attempts to access a resource belonging to a different `tenant_id`
- **THEN** the API returns a 404 Not Found (due to RLS hiding the row)

### Requirement: Rate Limiting
The system SHALL enforce a per-tenant token bucket rate limit using Redis, degrading gracefully if Redis is unavailable.

#### Scenario: Redis unavailable
- **WHEN** the API attempts to check the rate limit but the Redis server is unreachable
- **THEN** the system fails open and allows the request to proceed

### Requirement: Account Balance Endpoint
The system SHALL provide an HTTP API endpoint at `/api/v1/accounts/{id}/balance/` returning the account's current or point-in-time balance and metadata, and the core `/api/v1/accounts/{id}/` endpoint MUST return available and pending hold balances. For sharded accounts, these endpoints MUST return the aggregate balance of the master account and all its shards.

#### Scenario: Current balance query
- **WHEN** a client sends a GET request to `/api/v1/accounts/{id}/balance/`
- **THEN** the API returns the computed balance, normal balance orientation (Debit vs Credit), and `snapshot_used` boolean flag

#### Scenario: Temporal balance query with as_of ISO timestamp
- **WHEN** a client sends a GET request with `?as_of=2026-10-01T12:00:00Z`
- **THEN** the API returns the balance computed up to the specified timestamp

#### Scenario: Temporal balance query with as_of_entry UUID
- **WHEN** a client sends a GET request with `?as_of_entry=<uuid>`
- **THEN** the API returns the balance computed up to and including the specified entry sequence

#### Scenario: Zero-float monetary format
- **WHEN** returning the account balance response
- **THEN** all monetary values (`balance`, `debit_total`, `credit_total`) MUST be formatted strictly as string representations of `NUMERIC(28, 4)`

#### Scenario: Extended account response with holds
- **WHEN** a client requests account details from `/api/v1/accounts/{id}/`
- **THEN** the response includes `posted_balance`, `pending_holds`, and `available_balance` as strict string decimals instead of a single `balance` field

#### Scenario: Sharded account aggregate balance
- **WHEN** a client requests account details for a sharded account
- **THEN** the API transparently sums the balance of the master account and all its shards, returning the aggregate total

### Requirement: effective_at on journal entry endpoints
`POST /api/v1/journal-entries/` SHALL accept an optional ISO 8601 `effective_at` with an explicit UTC offset. Journal entry responses (create, replay, retrieve, reverse) and `entry.posted` / `entry.reversed` webhook payloads MUST include `effective_at`.

#### Scenario: Backdated create
- **WHEN** a client posts an entry with `"effective_at": "2026-09-30T23:00:00Z"` into an open period
- **THEN** the API returns 201 and the body's `effective_at` is `2026-09-30T23:00:00Z`

#### Scenario: Naive timestamp
- **WHEN** a client sends `effective_at` without a UTC offset
- **THEN** the API returns 400 with code `invalid`

#### Scenario: Future timestamp
- **WHEN** a client sends an `effective_at` later than the current time
- **THEN** the API returns 422 with code `invalid_effective_at`

#### Scenario: Closed period
- **WHEN** a client sends an `effective_at` before the tenant's cutoff
- **THEN** the API returns 422 with code `period_closed`

### Requirement: Period close endpoints
The system SHALL expose `POST /api/v1/period-closes/` to close the tenant's books before a `cutoff`, and `GET /api/v1/period-closes/` to return the tenant's current cutoff and its close history, newest first. `POST` MUST require an `Idempotency-Key` and replay stored responses like other mutating endpoints.

#### Scenario: Successful close
- **WHEN** a client posts `{"cutoff": "2026-10-01T00:00:00Z"}` with an `Idempotency-Key` and the cutoff is in the past and later than the current cutoff
- **THEN** the API returns 201 with the recorded close (`id`, `cutoff`, `source: "MANUAL"`, `created_at`)

#### Scenario: Idempotent replay
- **WHEN** the same close request is retried with the same `Idempotency-Key`
- **THEN** the API returns the original 201 body with `Idempotent-Replayed: true`, and no second close is recorded

#### Scenario: Invalid cutoff
- **WHEN** the cutoff is in the future, or not later than the current cutoff
- **THEN** the API returns 422 with code `invalid_period_cutoff`

#### Scenario: Listing closes
- **WHEN** a client sends `GET /api/v1/period-closes/`
- **THEN** the API returns `current_cutoff` (or `null`) and `results`, the tenant's closes ordered newest first, never including another tenant's closes

### Requirement: FX conversion endpoints
The system SHALL expose:
- `POST /api/v1/fx-conversions/` to convert between currencies
- `GET /api/v1/fx-conversions/` to list the tenant's conversions, newest first, at most 100
- `GET /api/v1/fx-conversions/{id}/` to retrieve one conversion

`POST` MUST require an `Idempotency-Key`, MUST accept `from_amount` and `rate` only as JSON strings, and MUST replay stored responses like other mutating endpoints.

#### Scenario: Successful conversion
- **WHEN** a client posts `from_account_id`, `to_account_id`, `from_amount` `"100.00"`, `rate` `"0.9234"` and a `reference_id`
- **THEN** the API returns 201 with the conversion id, journal entry id, both account ids and currencies, `from_amount` `100.0000`, `to_amount` `92.3400`, `rate`, `rate_source`, `rate_as_of`, `effective_at` and the entry's four lines (each with account, direction, amount and currency)

#### Scenario: Replay
- **WHEN** the same request is sent again with the same `Idempotency-Key`
- **THEN** the API returns the stored response with `Idempotent-Replayed: true`

#### Scenario: Missing idempotency key
- **WHEN** a client posts a conversion without an `Idempotency-Key`
- **THEN** the API returns 400 `missing_idempotency_key`

#### Scenario: Listing conversions
- **WHEN** a tenant has posted three conversions
- **THEN** `GET /api/v1/fx-conversions/` returns all three, newest first, each with its rate stamp

### Requirement: FX conversion error codes
The FX conversion endpoint SHALL return 422 for `same_currency`, `invalid_rate`, `invalid_rate_as_of`, `invalid_account`, `invalid_amount`, `insufficient_funds` and `period_closed`; 404 `not_found` for an unknown or other tenant's account or conversion; and 422 `idempotency_key_reuse` when a key is reused with a different body.

#### Scenario: Same-currency conversion
- **WHEN** a client converts between two accounts in the same currency
- **THEN** the API returns 422 with code `same_currency`

#### Scenario: Unknown account
- **WHEN** `to_account_id` does not exist for the tenant
- **THEN** the API returns 404 with code `not_found`
