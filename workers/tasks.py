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
    from ledger.services.tenancy import list_tenant_ids
    from django.db import connection, transaction

    tenant_ids = list_tenant_ids()

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


from ledger.services.snapshots import generate_account_snapshot

@shared_task(ignore_result=True)
def create_account_balance_snapshots():
    def run_for_tenant(tenant_id):
        # We find accounts with uncheckpointed lines > 50
        accounts_to_snapshot = []
        with connection.cursor() as cursor:
            query = """
                SELECT a.id 
                FROM accounts a
                LEFT JOIN balance_snapshots bs ON bs.account_id = a.id AND bs.sequence = (
                    SELECT MAX(sequence) FROM balance_snapshots WHERE account_id = a.id
                )
                JOIN transaction_lines l ON l.account_id = a.id
                JOIN journal_entries e ON e.id = l.journal_entry_id
                WHERE bs.sequence IS NULL OR e.sequence > bs.sequence
                GROUP BY a.id
                HAVING COUNT(l.id) > 50
            """
            cursor.execute(query)
            accounts_to_snapshot = [row[0] for row in cursor.fetchall()]
        
        # generate_account_snapshot creates its own transaction block internally
        for acc_id in accounts_to_snapshot:
            generate_account_snapshot(tenant_id, acc_id)
            
    for_each_tenant(run_for_tenant)

@shared_task(ignore_result=True)
def expire_holds():
    def run_for_tenant(tenant_id):
        from ledger.models import OutboxEvent
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE holds
                SET status = 'EXPIRED', updated_at = now()
                WHERE id IN (
                    SELECT id FROM holds
                    WHERE status = 'PENDING'
                      AND expires_at <= now()
                    ORDER BY expires_at
                    LIMIT 500
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, tenant_id, account_id, amount, reference_id, captured_amount
            """)
            expired_rows = cursor.fetchall()

        for row in expired_rows:
            OutboxEvent.objects.create(
                tenant_id=row[1],
                type="hold.expired",
                payload={
                    "hold_id": str(row[0]),
                    "account_id": str(row[2]),
                    "amount": str(row[3]),
                    "reference_id": row[4],
                    "released_amount": str(row[3] - row[5]),
                }
            )

    for_each_tenant(run_for_tenant)

ROLLUP_LOCK_ID = 827461928375

@shared_task(ignore_result=True)
def rollup_shards():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s);", [ROLLUP_LOCK_ID])
        lock_acquired = cursor.fetchone()[0]

    if not lock_acquired:
        return

    try:
        def run_for_tenant(tenant_id):
            from ledger.models import Account, Tenant
            from ledger.services.posting import post_entry, PostingError
            import uuid
            
            tenant = Tenant(id=tenant_id)
            
            shards = list(Account.objects.filter(tenant_id=tenant_id, parent_account__isnull=False).exclude(balance=0))
            
            for shard in shards:
                reference_id = f"rollup:{shard.id}:{shard.version}"
                
                if shard.type in ('ASSET', 'EXPENSE'):
                    if shard.balance > 0:
                        shard_dir = 'CREDIT'
                        master_dir = 'DEBIT'
                    else:
                        shard_dir = 'DEBIT'
                        master_dir = 'CREDIT'
                else:
                    if shard.balance > 0:
                        shard_dir = 'DEBIT'
                        master_dir = 'CREDIT'
                    else:
                        shard_dir = 'CREDIT'
                        master_dir = 'DEBIT'
                        
                amount = abs(shard.balance)
                
                payload = {
                    'reference_id': reference_id,
                    'description': 'Automated shard rollup',
                    'metadata': {'system': 'shard_rollup', 'shard_id': str(shard.id)},
                    'lines': [
                        {
                            'account_id': str(shard.id),
                            'amount': str(amount),
                            'direction': shard_dir
                        },
                        {
                            'account_id': str(shard.parent_account_id),
                            'amount': str(amount),
                            'direction': master_dir
                        }
                    ]
                }
                
                import json, hashlib
                idem_key = reference_id
                req_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).digest()
                
                try:
                    post_entry(tenant, payload, idem_key, req_hash, bypass_sharding=True)
                except PostingError as e:
                    logger.error(f"Failed to rollup shard {shard.id}: {str(e)}")

        for_each_tenant(run_for_tenant)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s);", [ROLLUP_LOCK_ID])

PERIOD_CLOSE_LOCK_ID = 827461928376

@shared_task(ignore_result=True)
def close_due_periods():
    """
    Scheduled month-end close for tenants with a close policy (tenants.period_close_grace_days).
    Idempotent: records a close only when the eligible cutoff is past the tenant's current one.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s);", [PERIOD_CLOSE_LOCK_ID])
        lock_acquired = cursor.fetchone()[0]

    if not lock_acquired:
        return

    try:
        from ledger.services.periods import close_due_period

        def run_for_tenant(tenant_id):
            # A close waits for the tenant's in-flight postings to drain.
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '5s'")
            close = close_due_period(tenant_id)
            if close:
                logger.info(f"Closed period for tenant {tenant_id} before {close.cutoff.isoformat()}")

        for_each_tenant(run_for_tenant)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s);", [PERIOD_CLOSE_LOCK_ID])

@shared_task
def sweep_canary():
    stats = for_each_tenant(lambda t: None)
    alert = False
    if stats['processed'] == 0:
        logger.error("Sweep canary alert: processed==0. Tenant enumeration is failing or silent no-op!")
        alert = True
    if stats['failed'] > 0:
        logger.error(f"Sweep canary alert: failed>0 ({stats['failed']}).")
        alert = True
    return {"alert": alert, **stats}
