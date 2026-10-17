# Spec Delta

## ADDED Requirements

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
