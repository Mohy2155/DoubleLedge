# Spec Delta

## ADDED Requirements

### Requirement: Per-currency balance before write
The posting service SHALL reject a journal entry whose debits and credits are unequal within any one currency, with `unbalanced_entry`, before writing anything, even when the entry balances across all currencies combined. The commit-time database trigger remains the backstop.

#### Scenario: Balanced overall but not per currency
- **WHEN** a client posts a debit of `10.0000` to a USD account and a credit of `10.0000` to a EUR account
- **THEN** the API returns 422 `unbalanced_entry` and no entry, line or idempotency record is stored

#### Scenario: Balanced in each currency
- **WHEN** a client posts an entry whose USD lines balance and whose EUR lines balance
- **THEN** the entry is posted
