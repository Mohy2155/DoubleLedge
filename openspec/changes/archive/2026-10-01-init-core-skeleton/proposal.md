# Proposal

## Why

DoubleLedge is a new project for a double-entry accounting engine with strict database-enforced invariants. We need to initialize the foundational repository structure, local environment orchestration, and the initial PostgreSQL schema to support the core constraints defined in the `README.md`. 

## What Changes

- Initialize `.gitignore` excluding standard Python/Django caches and agent artifacts.
- Create `.env.example` with standard database, Redis, Celery, and application settings.
- Scaffold the project directory layout: `ledger/migrations/`, `ledger/services/`, `api/`, `workers/`, `tests/`, `scripts/benchmarks/`.
- Configure `pyproject.toml` targeting Python 3.12 with dependencies for Django, DRF, PostgreSQL, Redis, Celery, Pydantic, and development tools.
- Set up Docker orchestration (`Dockerfile`, `docker-compose.yml`) for `db`, `redis`, `api`, `celery_worker`, and `celery_beat`.
- Create a `Makefile` supporting project operations (`up`, `down`, `migrate`, `test`, etc.).
- Initialize Django settings inside the root package (`config`).
- Create the initial `RunSQL` migration `ledger/migrations/0001_initial.py` to establish the raw DDL for `tenants`, `accounts`, `journal_entries`, `transaction_lines`, `idempotency_keys`, `outbox_events`, and `ledger_checkpoints`.
- Implement PostgreSQL constraints, deferred triggers for journal entry balance, immutability triggers, and Row-Level Security for tenant isolation within the initial migration.

## Capabilities

### New Capabilities
- `ledger/invariants`: Enforcing database-level correctness, including deferred triggers for balanced journal entries and immutability triggers.
- `tenant-isolation`: Row-Level Security (RLS) for isolating tenant data and composite foreign keys.
- `environment/orchestration`: Docker containerization, development environment variables, and build tools.

### Modified Capabilities

## Impact

- Establishes the foundational architecture and schema for all subsequent development on DoubleLedge.
- Sets the standard for database-first validation, requiring developers to work within the constraints of RLS and deferred triggers.
- Defines the local development workflow using Docker Compose and Make.
