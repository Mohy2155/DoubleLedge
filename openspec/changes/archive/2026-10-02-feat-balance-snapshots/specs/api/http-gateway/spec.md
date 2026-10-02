# Spec Delta

## ADDED Requirements

### Requirement: Account Balance Endpoint
The system SHALL provide an HTTP API endpoint at `/api/v1/accounts/{id}/balance/` returning the account's current or point-in-time balance and metadata.

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
