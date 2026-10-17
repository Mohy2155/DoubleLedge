import logging
import time
from contextlib import contextmanager

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, OperationalError, connections, transaction

logger = logging.getLogger(__name__)

REPLICA_ALIAS = 'replica'
CONSISTENCY_LEVELS = ('bounded', 'strong')
UNAVAILABLE_BACKOFF_SECONDS = 30

# Process-local circuit breaker: skip the replica for a while after a connection failure.
_unavailable_until = 0.0


def replica_status(start_lsn):
    """
    (caught_up, lag_seconds) for the replica relative to the primary's WAL position at request start.
    caught_up means every commit before the request started is visible on the replica.
    lag_seconds is None when the replica has replayed nothing yet (unknown, treated as stale).
    """
    with connections[REPLICA_ALIAS].cursor() as cursor:
        cursor.execute("""
            SELECT CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn() ELSE pg_current_wal_lsn() END
                       >= %s::pg_lsn,
                   EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp())
        """, [start_lsn])
        caught_up, lag = cursor.fetchone()
    return bool(caught_up), (float(lag) if lag is not None else None)


def choose_read_alias(consistency='bounded'):
    """Alias for a routed read: the replica when it is fresh enough for `consistency`, else the primary."""
    global _unavailable_until
    if not settings.READ_REPLICA_ENABLED or time.monotonic() < _unavailable_until:
        return DEFAULT_DB_ALIAS

    with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
        cursor.execute("SELECT pg_current_wal_lsn()")
        start_lsn = cursor.fetchone()[0]

    try:
        caught_up, lag = replica_status(start_lsn)
    except OperationalError:
        logger.warning("Read replica unavailable; serving reads from primary for %ss", UNAVAILABLE_BACKOFF_SECONDS)
        _unavailable_until = time.monotonic() + UNAVAILABLE_BACKOFF_SECONDS
        connections[REPLICA_ALIAS].close()
        return DEFAULT_DB_ALIAS

    if caught_up:
        return REPLICA_ALIAS
    if consistency == 'bounded' and lag is not None and lag <= settings.READ_REPLICA_MAX_LAG_SECONDS:
        return REPLICA_ALIAS
    return DEFAULT_DB_ALIAS


@contextmanager
def tenant_reads(tenant_id, consistency='bounded'):
    """
    Yields the alias to read from. RLS fails closed, so replica reads run in their own replica
    transaction with app.tenant_id set; the primary path relies on tenant_transaction() having set it.
    """
    alias = choose_read_alias(consistency)
    if alias == DEFAULT_DB_ALIAS:
        yield alias
        return
    with transaction.atomic(using=alias):
        with connections[alias].cursor() as cursor:
            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])
        yield alias
