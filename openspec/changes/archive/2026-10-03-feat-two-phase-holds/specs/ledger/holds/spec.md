# Spec Delta

## Purpose

Provides a two-phase commit pattern for ledger transactions, allowing funds to be reserved (authorized) and later captured or voided.

## ADDED Requirements

### Requirement: Authorize Hold
The system SHALL allow clients to reserve funds on an account by authorizing a hold, reducing the account's available balance without creating a journal entry.

#### Scenario: Successful authorization
- **WHEN** client provides a valid hold payload with an amount <= available balance
- **THEN** system creates a hold in PENDING status and returns 201

#### Scenario: Insufficient funds
- **WHEN** client attempts to authorize a hold exceeding available balance on an account that does not allow negative balances
- **THEN** system rejects the hold with HTTP 422 `insufficient_funds`

#### Scenario: Currency mismatch
- **WHEN** client specifies a currency different from the account's currency
- **THEN** system rejects the hold with HTTP 422 `currency_mismatch`

### Requirement: Capture Hold
The system SHALL allow capturing a pending hold, which creates a balanced journal entry settling the funds to a destination account.

#### Scenario: Full capture
- **WHEN** client captures a hold for its full authorized amount
- **THEN** system creates a journal entry, transitions hold to CAPTURED, and returns 201

#### Scenario: Partial capture
- **WHEN** client captures a hold for less than the authorized amount
- **THEN** system creates a journal entry for the captured amount, transitions hold to CAPTURED, and implicitly releases the remaining uncaptured funds

#### Scenario: Double spend prevention
- **WHEN** client attempts to capture a hold that is already CAPTURED, VOIDED, or EXPIRED
- **THEN** system rejects the capture with HTTP 409 `hold_not_pending`

### Requirement: Void Hold
The system SHALL allow voiding a pending hold, releasing the reserved funds immediately without creating any ledger entries.

#### Scenario: Void pending hold
- **WHEN** client voids a PENDING hold
- **THEN** system transitions the hold to VOIDED and releases the funds

### Requirement: Expiration
The system SHALL automatically expire pending holds that have passed their `expires_at` timestamp.

#### Scenario: Automatic expiration
- **WHEN** a background sweep process runs
- **THEN** it transitions all PENDING holds with `expires_at` in the past to EXPIRED status, releasing their funds
