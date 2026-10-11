# environment/orchestration Specification

## Purpose
Provides local development and testing environment orchestration via Docker Compose and Makefiles.

## Requirements

### Requirement: Local container orchestration
The project MUST provide a reproducible local environment using Docker Compose with pinned container images, an api healthcheck, an owner tools profile, and a one-command demo bootstrap.

#### Scenario: Bringing up the environment
- **WHEN** running `make up`
- **THEN** PostgreSQL, Redis, API, Celery worker, and Celery beat services are started and become healthy

#### Scenario: Running the one-command demo
- **WHEN** running `make demo`
- **THEN** development RSA keys are generated if missing, containers start with health checks, demo data is bootstrapped, and the access URL and token are output

### Requirement: Automated migrations and setup
The project MUST provide automated commands for database schema initialization.

#### Scenario: Running database migrations
- **WHEN** running `make migrate`
- **THEN** Django applies the initial schema, functions, triggers, and RLS policies to the PostgreSQL database without errors
