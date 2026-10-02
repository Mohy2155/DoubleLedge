import logging
from django.db import connection, transaction
from ledger.models import Tenant

logger = logging.getLogger(__name__)

def get_current_tenant():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.tenant_id', true)")
        return cursor.fetchone()[0]

def for_each_tenant(func):
    """
    Executes the given function for every tenant in the system.
    For each tenant, it opens a new transaction, sets the app.tenant_id config,
    and calls func(tenant_id).
    """
    tenants = Tenant.objects.all().values_list('id', flat=True)
    
    for tenant_id in tenants:
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])
                func(tenant_id)
        except Exception as e:
            logger.exception(f"Error processing tenant {tenant_id} in for_each_tenant loop: {e}")

def require_tenant_context():
    tenant_id = get_current_tenant()
    if not tenant_id:
        raise RuntimeError("No tenant context is set (app.tenant_id). This operation requires tenant isolation.")
    return tenant_id
