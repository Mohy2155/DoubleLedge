import pytest
import uuid
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta
from ledger.models import Tenant, Account
from ledger.services.holds import authorize_hold, capture_hold, void_hold, HoldError
from api.utils import generate_idempotency_hash
import json

def get_hash(payload):
    return generate_idempotency_hash('POST', '/api/v1/holds/', json.dumps(payload).encode())

@pytest.mark.django_db
def test_hold_idempotency():
    tenant = Tenant.objects.create(name="Tenant")
    account = Account.objects.create(
        tenant=tenant, name="Acc", type=Account.Type.LIABILITY, currency="USD", balance=Decimal("1000.00")
    )
    dest = Account.objects.create(
        tenant=tenant, name="Dest", type=Account.Type.REVENUE, currency="USD", balance=Decimal("0.00")
    )
    
    payload = {
        'reference_id': 'hold-idem',
        'account_id': str(account.id),
        'currency': 'USD',
        'amount': '100.00',
        'expires_at': (timezone.now() + timedelta(days=1)).isoformat()
    }
    idem_key = str(uuid.uuid4())
    req_hash = get_hash(payload)
    
    code1, body1, is_replayed1 = authorize_hold(tenant, payload, idem_key, req_hash)
    assert not is_replayed1
    assert code1 == 201
    
    code2, body2, is_replayed2 = authorize_hold(tenant, payload, idem_key, req_hash)
    assert is_replayed2
    assert code2 == 201
    assert body1 == body2
    
    # Different payload same key -> error
    req_hash_diff = get_hash({'different': 'payload'})
    with pytest.raises(HoldError, match="idempotency_key_reuse"):
        authorize_hold(tenant, payload, idem_key, req_hash_diff)
        
    hold_id = body1['hold_id']
    
    # Capture idempotency
    cap_payload = {
        'amount': '100.00',
        'destination_account_id': str(dest.id),
        'reference_id': 'capture-idem'
    }
    cap_idem_key = str(uuid.uuid4())
    cap_hash = get_hash(cap_payload)
    
    c1, b1, rep1 = capture_hold(tenant, hold_id, cap_payload, cap_idem_key, cap_hash)
    assert not rep1
    
    c2, b2, rep2 = capture_hold(tenant, hold_id, cap_payload, cap_idem_key, cap_hash)
    assert rep2
    assert b1 == b2
