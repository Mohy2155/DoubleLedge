# Spec Delta

## ADDED Requirements

### Requirement: Opt-in local read replica
The project SHALL provide an opt-in Compose override that runs a hot-standby streaming replica of the local database and points the API at it. One Makefile command MUST start it, it MUST work on an existing data volume, and the default `docker compose up` / `make demo` environment MUST remain unchanged.

#### Scenario: Starting the replica stack
- **WHEN** a developer runs `make replica`
- **THEN** a streaming replica is cloned from the running primary, becomes healthy, and the API serves eligible reads from it (`Read-Source: replica`)

#### Scenario: Default environment
- **WHEN** a developer runs `docker compose up` without the override
- **THEN** no replica container runs and the API reads only from the primary

#### Scenario: Primary recreated since the replica was cloned
- **WHEN** the primary's data is wiped and recreated while the replica volume survives, and the replica stack is started again
- **THEN** the replica detects that its database system identifier differs from the primary's, re-clones from the current primary, and the API never serves reads from the stale copy

#### Scenario: Wiping the environment
- **WHEN** a developer runs `make down`
- **THEN** the replica container and its volume are removed along with the primary's data
