# Spec Delta

## Purpose

Offloads statement and point-in-time balance reads to a streaming read replica, without weakening tenant isolation and only when the replica is provably fresh enough for the requested consistency.

## ADDED Requirements

### Requirement: Replica routing is opt-in
The system SHALL use a read replica only when one is configured. Without a configured replica, every read MUST be served by the primary, and the replica MUST NOT be contacted.

#### Scenario: No replica configured
- **WHEN** no replica is configured and a client requests an account statement
- **THEN** the primary serves the read and the response carries `Read-Source: primary`

### Requirement: Routed endpoints
Only `GET /api/v1/accounts/{id}/statement/` and `GET /api/v1/accounts/{id}/balance/` (with or without `as_of`) SHALL be eligible for replica routing. Their successful responses MUST include a `Read-Source` header whose value is `replica` or `primary`. All other endpoints, including every write, MUST use the primary.

#### Scenario: Header on routed endpoint
- **WHEN** a client requests an account balance
- **THEN** the response includes `Read-Source` naming the database that served the read

#### Scenario: Non-routed endpoint
- **WHEN** a client requests `GET /api/v1/accounts/{id}/`
- **THEN** the primary serves the read

### Requirement: Read-your-writes freshness
A routed read MAY be served by the replica whenever the replica has replayed all WAL the primary had written when the request started. Under any consistency level, such a read MUST reflect every write committed before the request began.

#### Scenario: Statement right after a posting
- **WHEN** a client posts an entry, the replica replays it, and the client then requests the statement
- **THEN** the replica serves the read and the statement includes the new entry

### Requirement: Bounded-staleness consistency
Under the default `consistency=bounded`, a routed read whose replica has not caught up to the request's start position MAY still be served by the replica if its replay lag is at most the configured maximum (default 5 seconds). Otherwise the primary MUST serve it. A replica whose lag cannot be determined MUST be treated as too stale.

#### Scenario: Replica lag within bound
- **WHEN** the replica is behind the request's start position by 1 second of replay lag and the maximum is 5 seconds
- **THEN** the replica serves the read

#### Scenario: Replica lag beyond bound
- **WHEN** the replica's replay lag exceeds the maximum
- **THEN** the primary serves the read

#### Scenario: Replication interrupted
- **WHEN** the replica has stopped receiving WAL while the primary keeps committing
- **THEN** the replica's lag grows past the bound and reads move to the primary

### Requirement: Strong consistency option
Routed endpoints SHALL accept `consistency=strong`, which MUST only accept a replica that has caught up to the request's start position. Otherwise the primary serves the read. Any `consistency` value other than `strong` or `bounded` MUST return `400` with code `invalid`.

#### Scenario: Strong read with lagging replica
- **WHEN** a client requests `?consistency=strong` and the replica is behind the request's start position
- **THEN** the primary serves the read

#### Scenario: Unknown consistency value
- **WHEN** a client sends `?consistency=eventual`
- **THEN** the API returns 400 with code `invalid`

### Requirement: Tenant isolation on replica reads
Replica reads MUST set the request's tenant context on the replica connection, so row-level security filters every replica query exactly as on the primary. A replica read MUST NOT return rows of another tenant, and MUST NOT return an empty result merely because the tenant context was missing.

#### Scenario: Statement served by the replica under the runtime role
- **WHEN** the replica serves a statement for an account with posted lines
- **THEN** the statement contains those lines, the same as when the primary serves it

#### Scenario: Another tenant's account on the replica
- **WHEN** a client requests the statement of an account belonging to a different tenant and the replica serves the read
- **THEN** the API returns 404

### Requirement: Fallback when the replica is unavailable
If the replica cannot be reached, routed reads MUST be served by the primary without returning an error. The system MUST then stop trying the replica for a back-off period (30 seconds), so the primary path does not pay a connection timeout on every request.

#### Scenario: Replica down
- **WHEN** the replica is unreachable and a client requests a balance
- **THEN** the API returns the balance from the primary with `Read-Source: primary`

#### Scenario: Back-off after failure
- **WHEN** a replica connection failed less than 30 seconds ago
- **THEN** routed reads go to the primary without attempting to contact the replica
