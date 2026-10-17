#!/bin/bash
# Entry point of the dev read replica (docker-compose.replica.yml). Runs as the postgres OS user.
set -eo pipefail

until pg_isready -h db -q; do sleep 1; done

# The replica volume can outlive the primary's (e.g. `docker compose down -v` with only the base file).
# Such a standby still answers queries, and its WAL position means nothing against the new primary, so
# it can pass the API's freshness check while holding another database. Re-clone it instead.
if [ -s "$PGDATA/PG_VERSION" ]; then
  primary_id=$(psql -h db -U postgres -d postgres -Atc "SELECT system_identifier FROM pg_control_system()")
  replica_id=$(pg_controldata -D "$PGDATA" | sed -n 's/^Database system identifier: *//p')
  if [ "$primary_id" != "$replica_id" ]; then
    echo "bootstrap: replica was cloned from database system $replica_id, primary is $primary_id; re-cloning"
    find "$PGDATA" -mindepth 1 -delete
  fi
fi

if [ ! -s "$PGDATA/PG_VERSION" ]; then
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

  # --checkpoint=fast: the default spread checkpoint paces the primary's dirty buffers over minutes
  # (73s after `make stress`), which outlasts this service's healthcheck and fails `make replica`.
  until pg_basebackup -h db -U postgres -D "$PGDATA" -R -X stream --checkpoint=fast; do sleep 1; done
  chmod 0700 "$PGDATA"
fi

exec postgres -c hot_standby_feedback=on
