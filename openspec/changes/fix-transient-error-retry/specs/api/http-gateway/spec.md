# Spec Delta

## MODIFIED Requirements

### Requirement: Error Mapping
The system SHALL map core domain exceptions and access control issues to standard HTTP status codes. When a request's transaction fails with a deadlock, serialization failure or lock timeout, the API SHALL retry the whole transaction up to 3 times before returning 503.

#### Scenario: Lock timeout occurs
- **WHEN** every attempt of a request's transaction fails with a deadlock, serialization failure or lock timeout
- **THEN** the API returns a 503 Service Unavailable with error code `lock_timeout` and a `Retry-After: 1` header

#### Scenario: Transient failure recovered
- **WHEN** the first attempt of a request's transaction fails with a deadlock and a retry succeeds
- **THEN** the client receives the normal response, and a mutating request's effects are applied exactly once

#### Scenario: Cross-tenant access
- **WHEN** a request attempts to access a resource belonging to a different `tenant_id`
- **THEN** the API returns a 404 Not Found (due to RLS hiding the row)
