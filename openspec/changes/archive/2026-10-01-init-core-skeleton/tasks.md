# Tasks

## 1. Environment & Ignores

- [x] 1.1 Create `.gitignore` ignoring standard Python/Django caches, test artifacts, `.env`, and agent artifacts (`.agents/`, `.agent/`, `skills/`, `.skills/`, `*.skill`, `.claude/`, `.cursor/`, `.copilot/`) and verify by inspecting the file.
- [x] 1.2 Create `.env.example` with standard database, Redis, Celery, and application settings and verify by inspecting the file.

## 2. Layout & Dependencies

- [x] 2.1 Create directory layout: `ledger/migrations/`, `ledger/services/`, `api/`, `workers/`, `tests/`, `scripts/benchmarks/` and verify the directories exist.
- [x] 2.2 Create `pyproject.toml` targeting Python 3.12 with dependencies: `django>=5.0,<5.1`, `djangorestframework>=3.15`, `psycopg[binary]>=3.1.18`, `redis>=5.0.3`, `celery>=5.3.6`, `pyjwt[crypto]>=2.8.0`, `pydantic>=2.6.4`, and dev dependencies (`pytest`, `pytest-django`, `hypothesis`, `ruff`, `mypy`), and verify by running `pip install -e .` (or equivalent).

## 3. Orchestration & Build

- [x] 3.1 Create `Dockerfile` using `python:3.12-slim` installing system packages (`build-essential`, `libpq-dev`, `curl`) and dependencies, and verify it builds successfully.
- [x] 3.2 Create `docker-compose.yml` defining services: `db` (PostgreSQL 16 with healthcheck), `redis` (Redis 7 with healthcheck), `api`, `celery_worker`, and `celery_beat`, and verify with `docker-compose config`.
- [x] 3.3 Create `Makefile` supporting targets: `up`, `down`, `migrate`, `test`, `test-race`, `bench`, `demo`, `integrity`, and `lint`, and verify the targets exist.

## 4. Django Config & Database Invariants

- [x] 4.1 Initialize Django settings inside the root package (`config`), ensuring `rest_framework`, `ledger`, and `api` are installed. Verify by running `python manage.py check`.
- [x] 4.2 Create `ledger/migrations/0001_initial.py` using `RunSQL` containing the raw DDL specified in README.md (tables, composite constraints, functions, triggers, RLS). Ensure floats are avoided and `NUMERIC(19,4)` is used. Verify by successfully running `python manage.py migrate` against the PostgreSQL 16 container.
- [x] 4.3 Bring up the containers via Docker Compose and verify `db` and `redis` are healthy.
