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
The system SHALL map core domain exceptions and access control issues to standard HTTP status codes.

#### Scenario: Lock timeout occurs
- **WHEN** the posting service throws a lock timeout exception
- **THEN** the API returns a 503 Service Unavailable with a `Retry-After: 1` header

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
