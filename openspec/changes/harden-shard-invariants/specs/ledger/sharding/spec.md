# Spec Delta

## ADDED Requirements

### Requirement: Immutable shard topology
The database MUST reject any change to an account's shard count or parent account after the account is created, whatever role or code path issues the change.

#### Scenario: Changing a sharded master's shard count
- **WHEN** any role updates the shard count of an account that has shards
- **THEN** the database rejects the update and the shard count is unchanged

#### Scenario: Re-parenting a shard
- **WHEN** any role updates a shard's parent account
- **THEN** the database rejects the update

#### Scenario: Other account updates
- **WHEN** a posting updates a sharded account's balance or version
- **THEN** the update succeeds

### Requirement: Shards allow negative balances
The database MUST reject any shard row, inserted or updated, whose `allow_negative` is false.

#### Scenario: Inserting a strict shard
- **WHEN** a shard is inserted with `allow_negative` false
- **THEN** the database rejects the insert

#### Scenario: Making a shard strict
- **WHEN** an existing shard is updated to `allow_negative` false
- **THEN** the database rejects the update
