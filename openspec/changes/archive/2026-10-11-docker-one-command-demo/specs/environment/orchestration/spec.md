# environment/orchestration Delta Specification

## MODIFIED Requirements

### Requirement: Local container orchestration
The project MUST provide a reproducible local environment using Docker Compose with pinned container images, an api healthcheck, an owner tools profile, and a one-command demo bootstrap.

#### Scenario: Bringing up the environment
- **WHEN** running `make up`
- **THEN** PostgreSQL, Redis, API, Celery worker, and Celery beat services are started and become healthy

#### Scenario: Running the one-command demo
- **WHEN** running `make demo`
- **THEN** development RSA keys are generated if missing, containers start with health checks, demo data is bootstrapped, and the access URL and token are output
