# ledger/period-close Specification

## Purpose
Lets a tenant freeze its books before a cutoff so no journal entry or line can ever be booked into an accounting period that has already been closed. The rule is enforced by the database, and closes can run on a per-tenant schedule.

## Requirements

### Requirement: Tenant period cutoff
Each tenant SHALL have at most one current cutoff: the latest cutoff recorded for that tenant, or none if no period has been closed. A journal entry is in a closed period when its `effective_at` is strictly earlier than the tenant's current cutoff.

#### Scenario: Tenant with no closes
- **WHEN** a tenant has never closed a period
- **THEN** the tenant has no cutoff and entries with any past `effective_at` are accepted

#### Scenario: Cutoff boundary is exclusive
- **WHEN** a tenant's cutoff is `2026-10-01T00:00:00Z` and an entry has `effective_at` exactly `2026-10-01T00:00:00Z`
- **THEN** the entry is in the open period and is accepted

#### Scenario: Cutoffs are per tenant
- **WHEN** tenant A closes through `2026-10-01T00:00:00Z`
- **THEN** tenant B can still post entries with `effective_at` in September 2026

### Requirement: Database-enforced period freeze
The database MUST reject any insert into `journal_entries` whose `effective_at` falls in the tenant's closed period, and any insert into `transaction_lines` whose parent entry does. This MUST hold for every database role and code path, not only for the HTTP API.

#### Scenario: Raw SQL insert into a closed period
- **WHEN** a journal entry with `effective_at` before the tenant's cutoff is inserted directly with SQL
- **THEN** the insert is rejected with a `check_violation` error whose message starts with `period_closed`

#### Scenario: Adding lines to an entry in a closed period
- **WHEN** a transaction line is inserted that references an existing entry whose `effective_at` is before the tenant's cutoff
- **THEN** the insert is rejected with a `check_violation` error whose message starts with `period_closed`

### Requirement: Monotonic, past-only closes
Recording a close MUST move the tenant's cutoff strictly forward, and the cutoff MUST NOT be later than the current time. Closes MUST be append-only: the runtime role cannot update or delete them, so a closed period cannot be reopened.

#### Scenario: Close moves cutoff backwards or repeats it
- **WHEN** a close is recorded with a cutoff equal to or earlier than the tenant's current cutoff
- **THEN** the database rejects it with a message starting with `invalid_period_cutoff`

#### Scenario: Close in the future
- **WHEN** a close is recorded with a cutoff later than the current time
- **THEN** the database rejects it with a message starting with `invalid_period_cutoff`

#### Scenario: Attempt to reopen a period
- **WHEN** any role issues `UPDATE`, `DELETE`, or `TRUNCATE` against recorded closes
- **THEN** the statement is rejected and the cutoff is unchanged

### Requirement: Race-free close against concurrent postings
A close and a concurrent posting for the same tenant MUST serialize, so every committed entry is consistent with the cutoff in force when it committed. No entry with `effective_at` before a cutoff may commit after that cutoff's close has committed.

#### Scenario: Posting in flight while a close commits
- **WHEN** a posting with `effective_at` before a new cutoff has inserted its entry but not yet committed, and a close for that cutoff starts
- **THEN** the close waits until the posting finishes, and the posting is treated as having happened before the close

#### Scenario: Posting starts while a close is in progress
- **WHEN** a close for a tenant is in progress and a posting for that tenant with `effective_at` before the new cutoff starts
- **THEN** the posting waits for the close to commit and is then rejected as `period_closed`

### Requirement: Corrections to closed periods
Entries in a closed period MUST stay reversible. The reversing entry SHALL be dated in the open period, so corrections never write into a closed period.

#### Scenario: Reversing an entry from a closed period
- **WHEN** a client reverses an entry whose `effective_at` is before the tenant's cutoff
- **THEN** the reversal is posted with `effective_at` set to the current time and succeeds

### Requirement: Scheduled monthly close
A tenant MAY have a close policy with a grace period in whole days (0–27). For every tenant with a policy, the system SHALL automatically close each completed UTC calendar month once that month's end plus the grace period has passed. Tenants without a policy MUST NOT be closed automatically.

#### Scenario: Grace period elapsed
- **WHEN** a tenant has a 5-day grace period and the scheduled close runs at `2026-11-06T00:30:00Z`
- **THEN** the tenant's cutoff becomes `2026-11-01T00:00:00Z`

#### Scenario: Grace period not yet elapsed
- **WHEN** a tenant has a 5-day grace period and the scheduled close runs at `2026-11-03T00:30:00Z`
- **THEN** the cutoff becomes at most `2026-10-01T00:00:00Z`, and October stays open

#### Scenario: Repeated runs are idempotent
- **WHEN** the scheduled close runs again after it has already closed the latest eligible month
- **THEN** no new close is recorded and no error is raised

#### Scenario: Catch-up after downtime
- **WHEN** the scheduler did not run for several months
- **THEN** the next run moves the cutoff directly to the latest eligible month boundary in a single close

#### Scenario: Tenant without a policy
- **WHEN** a tenant has no close policy
- **THEN** the scheduled close never records a close for that tenant
