import logging
from celery import shared_task
from django.db import connection
from ledger.services.reconciliation import run_global_reconciliation

logger = logging.getLogger(__name__)

# Advisory lock constant for reconciliation to prevent overlapping runs.
# Postgres advisory locks require a 64-bit integer.
RECONCILIATION_LOCK_ID = 827461928374

@shared_task(ignore_result=True)
def run_reconciliation_audit():
    """
    Background worker task to trigger the ledger reconciliation audit.
    Uses Postgres advisory locks to ensure only one worker runs this at a time.
    """
    with connection.cursor() as cursor:
        # Try to acquire a transaction-level advisory lock
        cursor.execute("SELECT pg_try_advisory_lock(%s);", [RECONCILIATION_LOCK_ID])
        lock_acquired = cursor.fetchone()[0]

    if not lock_acquired:
        logger.warning("Reconciliation audit already running. Skipping this execution.")
        return

    logger.info("Starting global ledger reconciliation audit.")
    try:
        run_global_reconciliation()
        logger.info("Global ledger reconciliation audit completed.")
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s);", [RECONCILIATION_LOCK_ID])
