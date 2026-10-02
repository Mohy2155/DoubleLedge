# Spec Delta

## ADDED Requirements

### Requirement: Transactional outbox event emission
The posting service MUST atomically record a system event whenever a journal entry is successfully created.

#### Scenario: Successful entry posting
- **WHEN** a journal entry is posted successfully
- **THEN** the system inserts an `OutboxEvent` (e.g., `entry.posted`) within the exact same database transaction as the entry lines

#### Scenario: Rollback on failure
- **WHEN** the journal entry creation fails or rolls back
- **THEN** no `OutboxEvent` is persisted, preventing phantom webhook dispatches
