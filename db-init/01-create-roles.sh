#!/bin/bash
set -e

# Note: This script only runs when Postgres initializes an empty data volume (/var/lib/postgresql/data).
# Changing passwords or role definitions later requires manual ALTER ROLE statements or recreating the volume (docker compose down -v).
# Note: ledger_tenant_lister only owns list_tenant_ids(); subsequent table permissions (e.g. SELECT on tenants) are granted by migration 0011.

# Fail loudly if LEDGER_RUNTIME_PASSWORD is not set
: "${LEDGER_RUNTIME_PASSWORD:?LEDGER_RUNTIME_PASSWORD must be set}"
PASSWORD=$LEDGER_RUNTIME_PASSWORD

# Reject characters that could cause SQL injection or syntax breakage inside the DO block
case "$PASSWORD" in
  *\'*|*\\*)
    echo "ERROR: LEDGER_RUNTIME_PASSWORD must not contain single quotes (') or backslashes (\\)" >&2
    exit 1
    ;;
esac

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  DO \$\$
  BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ledger_runtime') THEN
      CREATE ROLE ledger_runtime WITH LOGIN PASSWORD '${PASSWORD}' NOSUPERUSER NOBYPASSRLS NOCREATEDB;
    ELSE
      ALTER ROLE ledger_runtime WITH LOGIN PASSWORD '${PASSWORD}' NOSUPERUSER NOBYPASSRLS NOCREATEDB;
    END IF;

    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ledger_tenant_lister') THEN
      CREATE ROLE ledger_tenant_lister NOLOGIN NOINHERIT BYPASSRLS;
    END IF;
  END
  \$\$;

  -- Explicitly grant CONNECT to ledger_runtime in case PUBLIC CONNECT is revoked
  GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO ledger_runtime;

  -- Ensure schema creation on public is revoked from PUBLIC
  REVOKE CREATE ON SCHEMA public FROM PUBLIC;
EOSQL