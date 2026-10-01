# environment/orchestration Specification

## Purpose
Provides local development and testing environment orchestration via Docker Compose and Makefiles.

## Requirements

### Requirement: Local container orchestration
The project MUST provide a reproducible local environment using Docker Compose.

#### Scenario: Bringing up the environment
- **WHEN** running `make up`
- **THEN** PostgreSQL, Redis, API, Celery worker, and Celery beat services are started and become healthy

### Requirement: Automated migrations and setup
The project MUST provide automated commands for database schema initialization.

#### Scenario: Running database migrations
- **WHEN** running `make migrate`
- **THEN** Django applies the initial schema, functions, triggers, and RLS policies to the PostgreSQL database without errors
