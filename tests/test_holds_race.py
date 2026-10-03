import pytest
import uuid
import threading
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta
from django.db import connection
from ledger.models import Tenant, Account
from ledger.services.holds import authorize_hold, HoldError

def run_in_thread(func, *args):
    result = []
    def target():
        try:
            from django.db import connection
            connection.close()
            res = func(*args)
            result.append(("ok", res))
        except Exception as e:
            result.append(("error", e))
        finally:
            connection.close()
    t = threading.Thread(target=target)
    t.start()
    return t, result

import json
from api.utils import generate_idempotency_hash
def get_hash(payload):
    return generate_idempotency_hash('POST', '/api/v1/holds/', json.dumps(payload).encode())

@pytest.mark.django_db(transaction=True)
def test_authorize_hold_race():
    tenant = Tenant.objects.create(name="T3")
    account = Account.objects.create(
        tenant=tenant, name="Acc", type=Account.Type.LIABILITY, currency="USD", balance=Decimal("100.00"), allow_negative=False
    )
    
    def auth_thread(idx):
        payload = {
            'reference_id': f'h-{idx}',
            'account_id': str(account.id),
            'currency': 'USD',
            'amount': '40.00',
            'expires_at': (timezone.now() + timedelta(days=1)).isoformat()
        }
        return authorize_hold(tenant, payload, str(uuid.uuid4()), get_hash(payload))
        
    threads = []
    results = []
    for i in range(3): # Total 120, balance is 100, exactly 2 should succeed
        t, res = run_in_thread(auth_thread, i)
        threads.append(t)
        results.append(res)
        
    for t in threads:
        t.join()
        
    successes = 0
    insufficient = 0
    for res in results:
        status, val = res[0]
        if status == "ok":
            successes += 1
        elif status == "error" and isinstance(val, HoldError) and str(val) == "insufficient_funds":
            insufficient += 1
            
    assert successes == 2
    assert insufficient == 1
