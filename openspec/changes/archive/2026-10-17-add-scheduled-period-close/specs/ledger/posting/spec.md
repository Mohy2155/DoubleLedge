# Spec Delta

## ADDED Requirements

### Requirement: Business-time effective_at on postings
The posting service SHALL accept an optional `effective_at` (business time) for a new journal entry. When omitted, `effective_at` MUST default to the current time. A supplied `effective_at` MUST be timezone-aware and MUST NOT be later than the current time. `posted_at` (system time) is always set by the system.

#### Scenario: effective_at omitted
- **WHEN** an entry is posted without `effective_at`
- **THEN** the entry's `effective_at` equals its posting time, as before this change

#### Scenario: Backdated entry in an open period
- **WHEN** an entry is posted with an `effective_at` in the past that is not before the tenant's cutoff
- **THEN** the entry is posted with that `effective_at` and `posted_at` is the current time

#### Scenario: Future effective_at
- **WHEN** an entry is posted with an `effective_at` later than the current time
- **THEN** the posting is rejected as `invalid_effective_at` and nothing is written

### Requirement: Closed-period rejection before write
The posting service MUST reject an entry whose `effective_at` falls in the tenant's closed period as `period_closed`, before writing any row. The database freeze is the backstop if this check is bypassed.

#### Scenario: Backdated entry into a closed period
- **WHEN** an entry is posted with `effective_at` before the tenant's cutoff
- **THEN** the posting is rejected as `period_closed`, no entry, lines, outbox event, or balance change persists, and the idempotency key is not consumed
