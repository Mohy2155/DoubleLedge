import logging
import random
import time
from functools import wraps

from django.db import OperationalError, connection, transaction
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from api.authentication import TenantUser
from ledger.services.posting import transient_sqlstate

logger = logging.getLogger(__name__)

class LockTimeout(APIException):
    status_code = 503
    default_detail = 'Service unavailable due to lock timeout.'
    default_code = 'lock_timeout'
    
    def get_full_details(self):
        return {
            'error': {
                'code': self.default_code,
                'message': self.default_detail
            }
        }

def tenant_transaction(max_retries=3, base_delay=0.05):
    """
    Decorator for DRF views or view methods.
    1. Wraps view in transaction.atomic().
    2. Executes set_config('app.tenant_id', %s, true) for RLS.
    3. Retries the whole transaction on a transient error (deadlock, serialization failure,
       lock timeout), then answers 503 lock_timeout.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(*args, **kwargs):
            request = None
            for arg in args:
                if hasattr(arg, 'user'):
                    request = arg
                    break
            if not request and hasattr(args[0], 'request'):
                request = args[0].request
                
            if not request or not request.user or not isinstance(request.user, TenantUser):
                return Response({"error": {"code": "unauthorized", "message": "Authentication credentials were not provided."}}, status=401)
                
            tenant_id = request.user.id
            
            # Only the transaction's owner retries; ATOMIC_REQUESTS is off, so in production that is always us.
            can_retry = not connection.in_atomic_block
            retries = 0
            while True:
                try:
                    with transaction.atomic():
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])
                            cursor.execute("SET LOCAL lock_timeout = '2s'")

                        return view_func(*args, **kwargs)
                except OperationalError as e:
                    sqlstate = transient_sqlstate(e)
                    if not sqlstate:
                        raise
                    if can_retry and retries < max_retries:
                        retries += 1
                        time.sleep(base_delay * (2 ** retries) + random.uniform(0, 0.1))
                        logger.warning(f"Transaction retry {retries} for tenant {tenant_id} after {sqlstate}")
                    else:
                        raise LockTimeout()
        return _wrapped_view
    return decorator
