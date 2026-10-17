# Spec Delta

## ADDED Requirements

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
