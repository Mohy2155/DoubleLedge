import pytest
import uuid
import random
import time
import concurrent.futures
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta

from django.db import connection, connections
from ledger.models import Tenant, Account, Hold, JournalEntry
from ledger.services.holds import authorize_hold, capture_hold, HoldError
from workers.tasks import expire_holds
from api.utils import generate_idempotency_hash
import json

def get_hash(payload):
    return generate_idempotency_hash('POST', '/api/v1/holds/', json.dumps(payload).encode())

def close_connections():
    for conn in connections.all():
        conn.close()

@pytest.fixture
def stress_tenant():
    tenant = Tenant.objects.create(name="Stress Tenant")
    return tenant

@pytest.mark.django_db(transaction=True)
def test_thundering_herd_authorization(stress_tenant):
    account = Account.objects.create(
        tenant=stress_tenant,
        name="Thundering Herd Acc",
        type=Account.Type.LIABILITY,
        currency="USD",
        balance=Decimal("100.00"),
        allow_negative=False
    )
    
    def attempt_auth(thread_idx):
        close_connections() # Ensure fresh connection in thread
        payload = {
            'reference_id': f'herd-{thread_idx}',
            'account_id': str(account.id),
            'currency': 'USD',
            'amount': '100.00',
            'expires_at': (timezone.now() + timedelta(days=1)).isoformat()
        }
        idem_key = str(uuid.uuid4())
        req_hash = get_hash(payload)
        
        try:
            code, body, _ = authorize_hold(stress_tenant, payload, idem_key, req_hash)
            return ("SUCCESS", code)
        except HoldError as e:
            return ("ERROR", str(e))
        except Exception as e:
            return ("EXCEPTION", str(e))
        finally:
            close_connections()
            
    successes = 0
    insufficient = 0
    other_errors = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = [executor.submit(attempt_auth, i) for i in range(50)]
        for future in concurrent.futures.as_completed(futures):
            res_type, val = future.result()
            if res_type == "SUCCESS":
                successes += 1
            elif res_type == "ERROR" and val == "insufficient_funds":
                insufficient += 1
            else:
                other_errors.append((res_type, val))
                
    assert successes == 1, f"Expected exactly 1 success, got {successes}. Other errors: {other_errors}"
    assert insufficient == 49, f"Expected 49 insufficient funds, got {insufficient}"


@pytest.mark.django_db(transaction=True)
def test_double_spend_capture_race(stress_tenant):
    account = Account.objects.create(
        tenant=stress_tenant,
        name="Double Spend Acc",
        type=Account.Type.LIABILITY,
        currency="USD",
        balance=Decimal("100.00"),
        allow_negative=False
    )
    dest = Account.objects.create(
        tenant=stress_tenant,
        name="Double Spend Dest",
        type=Account.Type.REVENUE,
        currency="USD",
        balance=Decimal("0.00")
    )
    
    # Authorize one $50 hold
    payload = {
        'reference_id': 'race-auth',
        'account_id': str(account.id),
        'currency': 'USD',
        'amount': '50.00',
        'expires_at': (timezone.now() + timedelta(days=1)).isoformat()
    }
    code, body, _ = authorize_hold(stress_tenant, payload, str(uuid.uuid4()), get_hash(payload))
    hold_id = body['hold_id']
    
    def attempt_capture(thread_idx):
        close_connections()
        cap_payload = {
            'amount': '50.00',
            'destination_account_id': str(dest.id),
            'reference_id': f'race-cap-{thread_idx}'
        }
        idem_key = str(uuid.uuid4())
        cap_hash = get_hash(cap_payload)
        
        try:
            c, b, _ = capture_hold(stress_tenant, hold_id, cap_payload, idem_key, cap_hash)
            return ("SUCCESS", c)
        except HoldError as e:
            return ("ERROR", str(e))
        except Exception as e:
            return ("EXCEPTION", str(e))
        finally:
            close_connections()

    successes = 0
    not_pending = 0
    other_errors = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(attempt_capture, i) for i in range(20)]
        for future in concurrent.futures.as_completed(futures):
            res_type, val = future.result()
            if res_type == "SUCCESS":
                successes += 1
            elif res_type == "ERROR" and val == "hold_not_pending":
                not_pending += 1
            else:
                other_errors.append((res_type, val))
                
    assert successes == 1, f"Expected exactly 1 success, got {successes}. Errors: {other_errors}"
    assert not_pending == 19, f"Expected 19 not pending, got {not_pending}"
    
    # Verify exact single journal entry was created for this hold
    hold = Hold.objects.get(id=hold_id)
    assert hold.status == 'CAPTURED'
    assert JournalEntry.objects.filter(id=hold.journal_entry_id).count() == 1


@pytest.mark.django_db(transaction=True)
def test_sweep_vs_capture_contention(stress_tenant):
    account = Account.objects.create(
        tenant=stress_tenant,
        name="Sweep Contention Acc",
        type=Account.Type.LIABILITY,
        currency="USD",
        balance=Decimal("10000.00"),
        allow_negative=False
    )
    dest = Account.objects.create(
        tenant=stress_tenant,
        name="Sweep Contention Dest",
        type=Account.Type.REVENUE,
        currency="USD",
        balance=Decimal("0.00")
    )
    
    # Authorize 100 holds expiring NOW
    holds = []
    for i in range(100):
        h = Hold.objects.create(
            tenant=stress_tenant,
            reference_id=f"sweep-{i}",
            account=account,
            currency="USD",
            amount=Decimal("10.00"),
            expires_at=timezone.now()
        )
        holds.append(str(h.id))
        
    def worker_expire():
        close_connections()
        try:
            expire_holds()
            return ("EXPIRE_DONE", None)
        except Exception as e:
            return ("EXCEPTION", str(e))
        finally:
            close_connections()
            
    def worker_capture():
        close_connections()
        # Randomly select a hold from the batch
        hold_id = random.choice(holds)
        cap_payload = {
            'amount': '10.00',
            'destination_account_id': str(dest.id),
            'reference_id': f'sweep-cap-{uuid.uuid4()}'
        }
        idem_key = str(uuid.uuid4())
        cap_hash = get_hash(cap_payload)
        try:
            capture_hold(stress_tenant, hold_id, cap_payload, idem_key, cap_hash)
            return ("CAPTURE_SUCCESS", hold_id)
        except HoldError:
            # Expected if already expired or captured by another
            return ("CAPTURE_FAILED_EXPECTED", hold_id)
        except Exception as e:
            return ("EXCEPTION", str(e))
        finally:
            close_connections()

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=55) as executor:
        futures = []
        for _ in range(5):
            futures.append(executor.submit(worker_expire))
        for _ in range(50):
            futures.append(executor.submit(worker_capture))
            
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            
    exceptions = [r for r in results if r[0] == "EXCEPTION"]
    assert len(exceptions) == 0, f"Unexpected exceptions during concurrency: {exceptions}"
    
    # Assert every hold is in exactly one terminal state
    terminal_holds = Hold.objects.filter(id__in=holds, status__in=['CAPTURED', 'EXPIRED'])
    assert terminal_holds.count() == 100, f"Expected 100 terminal holds, found {terminal_holds.count()}"
    
    # No pending holds
    assert Hold.objects.filter(id__in=holds, status='PENDING').count() == 0
