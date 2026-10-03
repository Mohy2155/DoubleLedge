# Spec Delta

## Purpose

Manages hot-account shards, routing write traffic across shards, and periodically rolling up shard balances to the parent account.

## ADDED Requirements

### Requirement: Shard routing
The system SHALL route transactions targeting a sharded account to one of its shards.

#### Scenario: Transaction targeting a sharded account
- **WHEN** a transaction line targets an account that has `shard_count > 0`
- **THEN** the posting service deterministically or randomly selects one of the account's shards to use in place of the master account for locking and balance updates

### Requirement: Shard balance rollup
The system SHALL periodically sweep shard balances and collapse them into the master account.

#### Scenario: Scheduled rollup sweep
- **WHEN** the scheduled shard rollup task runs
- **THEN** it creates a journal entry moving the balance from each shard to the master account, bringing the shard's balance back to zero
