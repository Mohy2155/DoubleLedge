# Design

## Context

The initial scaffolding of the DoubleLedge project sets up the foundational database constraints and the application environment. DoubleLedge delegates correctness entirely to the database layer (PostgreSQL). We need to lay out the project directory, install dependencies, and build the Docker environment.

## Goals / Non-Goals

**Goals:**
- Enforce strict database invariants using PostgreSQL `RunSQL` migrations.
- Set up Row-Level Security (RLS) on all tenant-specific tables.
- Standardize development environments using Docker Compose and Makefiles.
- Establish the `config` root for Django settings.
- Implement strict constraints against the usage of floats in monetary contexts.

**Non-Goals:**
- Implementing application features, views, or models beyond the raw schema setup.
- Integrating external monitoring or OpenTelemetry (deferred to a later stage).

## Decisions

- **Raw SQL Migrations for Schema**: Rather than using Django's ORM to define the tables initially, we use a raw `RunSQL` migration (`ledger/migrations/0001_initial.py`).
  - *Rationale*: Django ORM cannot natively express deferred constraint triggers, composite foreign keys seamlessly mixed with RLS, or table-level `BEFORE TRUNCATE` immutable triggers. Establishing the exact DDL in SQL ensures we capture the invariants perfectly from day one.
- **Strictly `NUMERIC(19,4)` and `Decimal`**: Ban floats entirely.
  - *Rationale*: Monetary correctness. We will rely on PostgreSQL's `NUMERIC` and Python's `Decimal`.
- **Docker Compose Orchestration**: Manage PostgreSQL, Redis, and Celery via Compose.
  - *Rationale*: Ensures the entire multi-service architecture runs locally with one command (`make up`).

## Risks / Trade-offs

- **Risk: Django ORM friction with raw DDL**
  - *Mitigation*: We will build Django models later that map exactly to this schema using `managed = False` or carefully aligned fields.
- **Risk: Docker Compose overhead**
  - *Mitigation*: The environment is self-contained. `make down` will cleanly destroy it.
