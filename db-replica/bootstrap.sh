#!/bin/bash
# Entry point of the dev read replica (docker-compose.replica.yml). Runs as the postgres OS user.
set -e

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  until pg_isready -h db -q; do sleep 1; done

  # The image's default pg_hba.conf only allows local replication connections. Append a network
  # rule on the primary once, over SQL, so the primary container never has to be reconfigured.
  psql -h db -U postgres -d postgres -v ON_ERROR_STOP=1 -q <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_hba_file_rules
                  WHERE type = 'host' AND database = '{replication}' AND address = 'all') THEN
    EXECUTE format('COPY (SELECT %L) TO PROGRAM %L',
                   'host replication all all scram-sha-256',
                   'cat >> ' || current_setting('hba_file'));
    PERFORM pg_reload_conf();
  END IF;
END
$$;
SQL

  until pg_basebackup -h db -U postgres -D "$PGDATA" -R -X stream; do sleep 1; done
  chmod 0700 "$PGDATA"
fi

exec postgres -c hot_standby_feedback=on
