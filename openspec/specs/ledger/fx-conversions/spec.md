# ledger/fx-conversions Specification

## Purpose
Moves value between a tenant's accounts in different currencies with conversion entries that stay balanced in every currency, and permanently records the rate each conversion used.

## Requirements

### Requirement: Balanced conversion entry
A conversion SHALL post one journal entry with four lines. The source account decreases by `from_amount`, the destination account increases by `to_amount`, and each currency's leg is offset by the tenant's FX trading account for that currency, so the entry balances within each currency. "Decrease" and "increase" follow each account's normal balance side.

#### Scenario: USD asset to EUR asset
- **WHEN** a tenant converts `100.0000` from a USD asset account to a EUR asset account at rate `0.9234`
- **THEN** one entry is posted with: a credit of `100.0000` on the USD account, a debit of `100.0000` on FX Trading USD, a credit of `92.3400` on FX Trading EUR, and a debit of `92.3400` on the EUR account
- **AND** the USD account's balance falls by `100.0000` and the EUR account's balance rises by `92.3400`

#### Scenario: Liability source account
- **WHEN** a tenant converts from a USD liability account to a EUR liability account
- **THEN** the USD liability is debited (decreased) and the EUR liability is credited (increased), each offset by its currency's FX trading account

### Requirement: Converted amount rounding
The converted amount SHALL be `from_amount × rate` rounded half-up to 4 decimal places. A conversion whose converted amount rounds to zero MUST be rejected with `invalid_amount`. The database MUST reject a stored conversion whose `to_amount` does not follow this rule.

#### Scenario: Half-up rounding
- **WHEN** a tenant converts `1.0000` at rate `0.12345`
- **THEN** the converted amount is `0.1235`

#### Scenario: Converted amount rounds to zero
- **WHEN** a tenant converts `0.0001` at rate `0.0001`
- **THEN** the conversion is rejected with `invalid_amount` and nothing is written

#### Scenario: Inconsistent stamp at the database
- **WHEN** a conversion row is inserted directly with a `to_amount` that differs from `from_amount × rate` rounded half-up to 4 places
- **THEN** the database rejects the insert

### Requirement: FX trading accounts
Each tenant SHALL have at most one FX trading account per currency, named `FX Trading <CCY>`. It is an `EQUITY` account whose balance may be negative. It is created automatically by the first conversion that needs it, and later conversions reuse it.

#### Scenario: First conversion in a currency pair
- **WHEN** a tenant with no FX trading accounts performs its first USD→EUR conversion
- **THEN** `FX Trading USD` and `FX Trading EUR` exist afterwards and appear in the tenant's account list

#### Scenario: Concurrent first conversions
- **WHEN** two USD→EUR conversions for the same tenant run concurrently and neither trading account exists yet
- **THEN** both conversions succeed and the tenant has exactly one `FX Trading USD` and one `FX Trading EUR`

### Requirement: Rate stamp
Each conversion SHALL record its source and destination accounts and currencies, `from_amount`, `to_amount`, the exact `rate` supplied, `rate_source` (empty if omitted), `rate_as_of`, and its journal entry. `rate_as_of` defaults to the time of the request.

#### Scenario: Stamp retrieved later
- **WHEN** a conversion was posted with rate `0.9234`, `rate_source` `ECB` and `rate_as_of` `2026-10-16T16:00:00Z`
- **THEN** retrieving that conversion returns exactly those values together with its journal entry id

#### Scenario: rate_as_of omitted
- **WHEN** a conversion is posted without `rate_as_of`
- **THEN** its `rate_as_of` is the time the conversion was processed

### Requirement: Conversion input validation
A conversion MUST be rejected before anything is written if:
- the source and destination accounts share a currency (`same_currency`)
- the rate is not a positive decimal string with at most 12 decimal places (`invalid_rate`)
- `rate_as_of` is in the future (`invalid_rate_as_of`)
- either account is an FX trading account (`invalid_account`)

#### Scenario: Same currency
- **WHEN** a tenant converts between two USD accounts
- **THEN** the conversion is rejected with `same_currency`

#### Scenario: Invalid rate
- **WHEN** the rate is `0`, negative, a JSON number, or has 13 decimal places
- **THEN** the conversion is rejected with `invalid_rate`

#### Scenario: Trading account as destination
- **WHEN** the destination account is the tenant's `FX Trading EUR`
- **THEN** the conversion is rejected with `invalid_account`

### Requirement: Conversions follow posting rules
A conversion's entry SHALL be subject to every rule that applies to journal entries:
- idempotency keyed on the client's `Idempotency-Key`
- `effective_at` validation and closed-period rejection
- the non-negative balance constraint
- shard routing
- the `entry.posted` outbox event

A rejected conversion MUST write nothing.

#### Scenario: Insufficient funds
- **WHEN** a conversion would take a non-negative source account below zero
- **THEN** it is rejected with `insufficient_funds` and no entry, conversion record or trading account is written

#### Scenario: Closed period
- **WHEN** a conversion's `effective_at` is before the tenant's period-close cutoff
- **THEN** it is rejected with `period_closed`

#### Scenario: Idempotent replay
- **WHEN** a conversion request is repeated with the same `Idempotency-Key` and body
- **THEN** the original conversion is returned and no second entry is posted

#### Scenario: Webhook
- **WHEN** a conversion is posted
- **THEN** an `entry.posted` event is emitted for its journal entry

### Requirement: Immutable conversion history
The database MUST reject any update, delete or truncate of conversion records.

#### Scenario: Attempted edit
- **WHEN** any role attempts to update a conversion's rate
- **THEN** the database rejects the statement

### Requirement: Conversion tenant isolation
Conversion records SHALL be visible only to their own tenant. A conversion MUST NOT reference an account or journal entry of another tenant.

#### Scenario: Another tenant's conversion
- **WHEN** a tenant retrieves a conversion id belonging to another tenant
- **THEN** it is not found

#### Scenario: Another tenant's account
- **WHEN** a tenant names another tenant's account as the destination
- **THEN** the conversion is rejected as not found and nothing is written

### Requirement: Reversing a conversion
Reversing a conversion's journal entry SHALL restore all four accounts' balances. The conversion record MUST remain unchanged.

#### Scenario: Reversal
- **WHEN** the journal entry of a USD→EUR conversion is reversed
- **THEN** the source, destination and both FX trading accounts return to their balances before the conversion, and the conversion is still retrievable with its original stamp
