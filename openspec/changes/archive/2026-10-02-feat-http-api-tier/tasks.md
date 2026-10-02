# Tasks

## 1. Setup & Auth

- [x] 1.1 Configure DRF and `drf-spectacular` in `config/settings.py`, and implement structured JSON logging. Verify server starts successfully.
- [x] 1.2 Implement RS256 JWT DRF authentication class (requiring `exp` and `tenant_id` UUID). Add `PyJWT[crypto]` dependency. Verify JWT validation logic.
- [x] 1.3 Create a view-level `transaction.atomic()` wrapper that executes `SELECT set_config('app.tenant_id', %s, true)` and includes deadlock retry logic. Write tests in `tests/test_api_auth.py` verifying that missing/expired/cross-tenant tokens fail closed. Verify tests pass.

## 2. Infrastructure & Serializers

- [x] 2.1 Implement `api.exceptions.custom_exception_handler` translating domain/DB exceptions to HTTP 400, 401, 404, 409, 422, 429, and 503. Verify with unit tests.
- [x] 2.2 Build request serializers with strict zero-float validation (using a custom JSON parser hook or explicit `isinstance(str)` field checks). Verify with serializer tests.
- [x] 2.3 Implement canonical JSON hashing (method + path + canonical UTF-8 body with sorted keys and compact separators) for the SHA-256 idempotency request hash. Verify hashes match correctly.

## 3. Endpoints

- [x] 3.1 Implement `POST /api/v1/journal-entries/` (delegating to `post_entry`, attaching `Idempotent-Replayed: true` when replaying). Ensure missing `Idempotency-Key` header returns 400. Verify entry creation.
- [x] 3.2 Implement `GET /api/v1/journal-entries/{id}/`. Verify entry and lines are retrieved correctly, and cross-tenant access returns 404.
- [x] 3.3 Implement `POST /api/v1/journal-entries/{id}/reverse/`. Accept or derive a `reference_id` for the reversal. Ensure `already_reversed` returns 409. Verify successful reversal and idempotency.
- [x] 3.4 Implement `GET /api/v1/accounts/{id}/`. Verify account details and cached balance are returned.
- [x] 3.5 Implement `GET /api/v1/accounts/{id}/balance/?as_of=<ts>`. Use timezone-aware `effective_at` for point-in-time computation. Verify computation from lines.
- [x] 3.6 Implement `GET /api/v1/accounts/{id}/statement/` with cursor pagination and running balance. Verify correct line pagination and balance rolling.

## 4. Rate Limiting & Docs

- [x] 4.1 Implement custom per-tenant Redis token bucket rate limiting (failing open if Redis is down). Verify rate limit headers and 429 status codes on exhaustion.
- [x] 4.2 Configure OpenAPI schema generation and Swagger UI at `/api/docs/` using `drf-spectacular`. Verify the `/api/docs/` URL loads correctly.
- [x] 4.3 Run `docker compose exec api pytest tests/ -v` to confirm all tests pass successfully.
