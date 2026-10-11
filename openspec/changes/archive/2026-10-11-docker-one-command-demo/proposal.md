# Proposal: Docker One-Command Demo

## Why
Enable a zero-configuration, robust one-command demo (`make demo`) that orchestrates RSA key generation, container startup with healthchecks, and idempotent tenant/data bootstrapping without hardcoded credentials or manual intervention.

## What Changes
- Pin base image versions: postgres:16.15, redis:7.4.11, python:3.12.15-slim.
- Introduce `owner` service in `docker-compose.yml` with tools profile and admin db privileges for maintenance tasks.
- Add healthcheck for `api` service.
- Add `scripts/gen_dev_keys.py` for standalone RSA keypair generation.
- Add `demo_bootstrap` management command for single-step tenant creation and demo seeding.
- Streamline `Makefile` targets (`up`, `stop`, `down`, `demo`, `test`, etc.) removing obsolete commands.

## Out of Scope
- Direct changes to `openspec/specs/`.
- Changes to API business logic or models.
