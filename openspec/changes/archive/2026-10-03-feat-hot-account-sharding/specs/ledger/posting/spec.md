# Spec Delta

## MODIFIED Requirements

### Requirement: Deterministic concurrency control
The posting service MUST prevent lock-ordering deadlocks between concurrent postings touching overlapping accounts, and MUST distribute contention for sharded accounts by locking shards instead of the master account.

#### Scenario: Concurrent opposing transfers
- **WHEN** multiple concurrent transactions attempt to transfer between the same accounts in opposing directions
- **THEN** the posting service avoids cross-locks by explicitly sorting and locking accounts in ascending primary-key order

#### Scenario: Posting to a sharded account
- **WHEN** a payload targets a sharded master account
- **THEN** the posting service deterministically or randomly maps the target to one of the master account's shards and acquires the row lock on the shard instead of the master account
