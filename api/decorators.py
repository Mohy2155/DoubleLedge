import logging
import random
import time
from functools import wraps

from django.db import OperationalError, connection, transaction
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from api.authentication import TenantUser

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
    3. Catches deadlock/lock_timeout and retries.
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
            
            retries = 0
            while True:
                try:
                    with transaction.atomic():
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])
                            cursor.execute("SET LOCAL lock_timeout = '2s'")
                            
                        return view_func(*args, **kwargs)
                except OperationalError as e:
                    cause = getattr(e, '__cause__', None)
                    pgcode = getattr(cause, 'pgcode', None)
                    # 40P01 is deadlock_detected, 55P03 is lock_not_available
                    if pgcode in ('40P01', '55P03') and retries < max_retries:
                        retries += 1
                        time.sleep(base_delay * (2 ** retries) + random.uniform(0, 0.1))
                        logger.warning(f"Transaction retry {retries} for tenant {tenant_id}")
                    elif pgcode in ('40P01', '55P03'):
                        raise LockTimeout()
                    else:
                        raise
        return _wrapped_view
    return decorator
