# tenant-isolation Specification

## Purpose
Enforces strong data isolation between tenants using Row-Level Security (RLS) and composite foreign keys to prevent data leakage.

## Requirements

### Requirement: Row-Level Security isolation
The database MUST enforce Row-Level Security on all tenant tables, failing closed if no tenant context is set.

#### Scenario: Querying data without tenant context
- **WHEN** a query is executed without setting the app.tenant_id local variable
- **THEN** the query returns zero rows for any tenant table

#### Scenario: Querying data with tenant context
- **WHEN** a query is executed with app.tenant_id set to tenant A
- **THEN** only rows belonging to tenant A are returned

### Requirement: Composite foreign key protection
Transaction lines MUST reference accounts within the same tenant using composite foreign keys.

#### Scenario: Attempting cross-tenant account reference
- **WHEN** inserting a transaction line for tenant A pointing to an account belonging to tenant B
- **THEN** the database rejects the insert due to a foreign key violation
