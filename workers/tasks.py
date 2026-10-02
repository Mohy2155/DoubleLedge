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

from ledger.services.webhooks import (
    fan_out_events,
    claim_deliveries,
    send_webhook,
    record_delivery_result,
    WebhookDispatchError,
)
from ledger.services.tenancy import for_each_tenant

@shared_task(ignore_result=True)
def dispatch_webhooks():
    """
    Background worker task to fan-out and dispatch webhooks.

    Transaction boundaries (R-2 fix):
      - fan_out_events() commits its own transaction before returning.
      - claim_deliveries() commits its own transaction (sweep + lease) before returning.
      - HTTP dispatch (send_webhook) runs with NO open DB transaction.
      - record_delivery_result() opens and commits its own transaction per delivery.

    This means no DB connection is held idle during network I/O.
    """
    def run_for_tenant(tenant_id):
        # Step 1: Fan out pending outbox events → webhook_deliveries.
        # for_each_tenant wraps this in transaction.atomic(); fan_out_events
        # opens a nested savepoint.  Both commit when run_for_tenant returns.
        fan_out_events(batch_size=100)

    # Fan-out runs inside for_each_tenant's transaction; it commits per tenant.
    for_each_tenant(run_for_tenant)

    # Step 2: Claim and dispatch — done *outside* for_each_tenant so each
    # claim+HTTP+record cycle has its own tight transaction boundary.
    from ledger.models import Tenant
    from django.db import connection, transaction

    tenant_ids = list(Tenant.objects.values_list('id', flat=True))

    for tenant_id in tenant_ids:
        try:
            # Set tenant context for claim_deliveries and record_delivery_result.
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('app.tenant_id', %s, true)",
                        [str(tenant_id)],
                    )
                # claim_deliveries opens its own nested savepoint; commits on exit.
                claimed_list = claim_deliveries(batch_size=10, lease_seconds=60)
            # ↑ Transaction committed. No DB lock held from here down.

            for claimed in claimed_list:
                try:
                    response = send_webhook(claimed.delivery)   # pure network I/O
                    success = 200 <= response.status_code < 300
                    with transaction.atomic():
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT set_config('app.tenant_id', %s, true)",
                                [str(tenant_id)],
                            )
                        record_delivery_result(
                            claimed,
                            success=success,
                            status_code=response.status_code,
                        )
                except WebhookDispatchError as exc:
                    with transaction.atomic():
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT set_config('app.tenant_id', %s, true)",
                                [str(tenant_id)],
                            )
                        record_delivery_result(claimed, success=False, error=str(exc))
                except Exception:
                    logger.exception(
                        "Unexpected error dispatching webhook delivery=%s",
                        claimed.delivery.id,
                    )
                    with transaction.atomic():
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT set_config('app.tenant_id', %s, true)",
                                [str(tenant_id)],
                            )
                        record_delivery_result(
                            claimed, success=False, error="Unexpected internal error"
                        )

        except Exception:
            logger.exception("Error in dispatch loop for tenant=%s", tenant_id)

@shared_task(ignore_result=True)
def prune_webhooks():
    from ledger.services.webhooks import prune_old_events_and_deliveries
    def run_for_tenant(tenant_id):
        prune_old_events_and_deliveries(days=7)
    for_each_tenant(run_for_tenant)

