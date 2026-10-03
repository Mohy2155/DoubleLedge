import pytest
import uuid
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta
from django.db.models import Sum, F
from ledger.models import Tenant, Account, Hold, OutboxEvent, JournalEntry
from ledger.services.holds import authorize_hold, capture_hold, void_hold, HoldError
from ledger.services.posting import post_entry
from api.utils import generate_idempotency_hash

def setup_test_data():
    tenant = Tenant.objects.create(name="Test Tenant")
    asset_account = Account.objects.create(
        tenant=tenant, name="Customer Wallet", type=Account.Type.LIABILITY, currency="USD", balance=Decimal("1000.00")
    )
    revenue_account = Account.objects.create(
        tenant=tenant, name="Revenue", type=Account.Type.REVENUE, currency="USD", balance=Decimal("0.00")
    )
    return tenant, asset_account, revenue_account

def get_hash(payload):
    import json
    return generate_idempotency_hash('POST', '/api/v1/holds/', json.dumps(payload).encode())

@pytest.mark.django_db
def test_hold_lifecycle():
    tenant, account, dest = setup_test_data()
    expires_at = timezone.now() + timedelta(days=1)
    
    # 1. Authorize
    payload = {
        'reference_id': 'hold-1',
        'account_id': str(account.id),
        'currency': 'USD',
        'amount': '200.00',
        'expires_at': expires_at.isoformat()
    }
    idem_key = str(uuid.uuid4())
    req_hash = get_hash(payload)
    
    code, body, is_replayed = authorize_hold(tenant, payload, idem_key, req_hash)
    assert code == 201
    assert body['status'] == 'PENDING'
    hold_id = body['hold_id']
    
    hold = Hold.objects.get(id=hold_id)
    assert hold.status == 'PENDING'
    
    # Check outbox
    assert OutboxEvent.objects.filter(type="hold.authorized", payload__hold_id=hold_id).exists()
    
    # Available balance should be reduced
    from ledger.services.holds import compute_available_balance
    from django.db.models import Sum, F
    pending = Hold.objects.filter(account=account, status='PENDING').aggregate(t=Sum(F('amount')-F('captured_amount')))['t']
    assert compute_available_balance(account, pending) == Decimal("800.00")
    
    # 2. Capture
    capture_payload = {
        'amount': '200.00',
        'destination_account_id': str(dest.id),
        'reference_id': 'capture-1'
    }
    cap_idem_key = str(uuid.uuid4())
    cap_hash = get_hash(capture_payload)
    
    code, cap_body, is_replayed = capture_hold(tenant, hold_id, capture_payload, cap_idem_key, cap_hash)
    assert code == 201
    
    hold.refresh_from_db()
    assert hold.status == 'CAPTURED'
    assert hold.journal_entry_id is not None
    
    je = JournalEntry.objects.get(id=hold.journal_entry_id)
    assert je.lines.count() == 2
    
    account.refresh_from_db()
    assert account.balance == Decimal("800.00") # Decreased by 200
    
    assert OutboxEvent.objects.filter(type="hold.captured", payload__hold_id=hold_id).exists()

@pytest.mark.django_db
def test_void_hold():
    tenant, account, dest = setup_test_data()
    payload = {
        'reference_id': 'hold-void',
        'account_id': str(account.id),
        'currency': 'USD',
        'amount': '200.00',
        'expires_at': (timezone.now() + timedelta(days=1)).isoformat()
    }
    code, body, _ = authorize_hold(tenant, payload, str(uuid.uuid4()), get_hash(payload))
    hold_id = body['hold_id']
    
    code, body, _ = void_hold(tenant, hold_id, str(uuid.uuid4()), get_hash({}))
    assert code == 200
    
    hold = Hold.objects.get(id=hold_id)
    assert hold.status == 'VOIDED'
    
    assert OutboxEvent.objects.filter(type="hold.voided", payload__hold_id=hold_id).exists()
    
    pending = Hold.objects.filter(account=account, status='PENDING').aggregate(t=Sum(F('amount')-F('captured_amount')))['t'] or Decimal('0')
    from ledger.services.holds import compute_available_balance
    assert compute_available_balance(account, pending) == Decimal("1000.00")

@pytest.mark.django_db
def test_partial_capture():
    tenant, account, dest = setup_test_data()
    payload = {
        'reference_id': 'hold-partial',
        'account_id': str(account.id),
        'currency': 'USD',
        'amount': '500.00',
        'expires_at': (timezone.now() + timedelta(days=1)).isoformat()
    }
    code, body, _ = authorize_hold(tenant, payload, str(uuid.uuid4()), get_hash(payload))
    hold_id = body['hold_id']
    
    capture_payload = {
        'amount': '300.00',
        'destination_account_id': str(dest.id),
        'reference_id': 'capture-partial'
    }
    code, cap_body, _ = capture_hold(tenant, hold_id, capture_payload, str(uuid.uuid4()), get_hash(capture_payload))
    
    assert Decimal(cap_body['captured_amount']) == Decimal('300.00')
    assert Decimal(cap_body['released_amount']) == Decimal('200.00')
    
    hold = Hold.objects.get(id=hold_id)
    assert hold.captured_amount == Decimal("300.00")
    
    je = JournalEntry.objects.get(id=hold.journal_entry_id)
    assert je.lines.filter(amount=Decimal("300.00")).exists()
    
    with pytest.raises(HoldError, match="hold_not_pending"):
        capture_hold(tenant, hold_id, capture_payload, str(uuid.uuid4()), get_hash(capture_payload))

@pytest.mark.django_db
def test_insufficient_funds():
    tenant = Tenant.objects.create(name="T2")
    account = Account.objects.create(
        tenant=tenant, name="Acc", type=Account.Type.LIABILITY, currency="USD", balance=Decimal("100.00"), allow_negative=False
    )
    
    payload = {
        'reference_id': 'h1',
        'account_id': str(account.id),
        'currency': 'USD',
        'amount': '100.00',
        'expires_at': (timezone.now() + timedelta(days=1)).isoformat()
    }
    authorize_hold(tenant, payload, str(uuid.uuid4()), get_hash(payload))
    
    payload2 = {
        'reference_id': 'h2',
        'account_id': str(account.id),
        'currency': 'USD',
        'amount': '1.00',
        'expires_at': (timezone.now() + timedelta(days=1)).isoformat()
    }
    with pytest.raises(HoldError, match="insufficient_funds"):
        authorize_hold(tenant, payload2, str(uuid.uuid4()), get_hash(payload2))
        
    account.allow_negative = True
    account.save()
    
    code, body, _ = authorize_hold(tenant, payload2, str(uuid.uuid4()), get_hash(payload2))
    assert code == 201

@pytest.mark.django_db
def test_state_machine_enforcement():
    tenant, account, dest = setup_test_data()
    
    # Void a hold, then try to capture
    payload = {
        'reference_id': 'hm1',
        'account_id': str(account.id),
        'currency': 'USD',
        'amount': '10.00',
        'expires_at': (timezone.now() + timedelta(days=1)).isoformat()
    }
    code, body, _ = authorize_hold(tenant, payload, str(uuid.uuid4()), get_hash(payload))
    hold_id = body['hold_id']
    
    void_hold(tenant, hold_id, str(uuid.uuid4()), get_hash({}))
    
    capture_payload = {'destination_account_id': str(dest.id), 'reference_id': 'c1'}
    with pytest.raises(HoldError, match="hold_not_pending"):
        capture_hold(tenant, hold_id, capture_payload, str(uuid.uuid4()), get_hash(capture_payload))

    # Test DB trigger
    from django.db import connection, InternalError, ProgrammingError
    with pytest.raises((InternalError, ProgrammingError)):
        with connection.cursor() as cursor:
            cursor.execute("UPDATE holds SET status='PENDING' WHERE id=%s", [hold_id])
