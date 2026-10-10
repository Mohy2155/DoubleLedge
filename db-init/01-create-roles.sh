#!/bin/bash
set -e

# Ledger runtime role requires LEDGER_RUNTIME_PASSWORD to be set
PASSWORD=${LEDGER_RUNTIME_PASSWORD:-app_password}

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
EOSQL
