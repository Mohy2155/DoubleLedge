import pytest
import uuid
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta
from ledger.models import Tenant, Account, Hold, OutboxEvent
from workers.tasks import expire_holds

@pytest.mark.django_db(transaction=True)
def test_expire_holds():
    tenant = Tenant.objects.create(name="Tenant")
    account = Account.objects.create(
        tenant=tenant, name="Acc", type=Account.Type.LIABILITY, currency="USD", balance=Decimal("100.00")
    )
    
    # 1. Hold in the past
    h_past = Hold.objects.create(
        tenant=tenant, reference_id="past", account=account, currency="USD",
        amount=Decimal("10.00"), expires_at=timezone.now() - timedelta(minutes=1)
    )
    
    # 2. Hold in the future
    h_future = Hold.objects.create(
        tenant=tenant, reference_id="future", account=account, currency="USD",
        amount=Decimal("10.00"), expires_at=timezone.now() + timedelta(minutes=1)
    )
    
    # 3. CAPTURED hold in the past
    h_captured = Hold.objects.create(
        tenant=tenant, reference_id="captured_past", account=account, currency="USD",
        amount=Decimal("10.00"), captured_amount=Decimal("10.00"), status="CAPTURED", 
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    
    expire_holds()
    
    h_past.refresh_from_db()
    assert h_past.status == "EXPIRED"
    assert OutboxEvent.objects.filter(type="hold.expired", payload__hold_id=str(h_past.id)).exists()
    
    h_future.refresh_from_db()
    assert h_future.status == "PENDING"
    
    h_captured.refresh_from_db()
    assert h_captured.status == "CAPTURED"
    
    # Idempotency run
    outbox_count = OutboxEvent.objects.filter(type="hold.expired").count()
    expire_holds()
    assert OutboxEvent.objects.filter(type="hold.expired").count() == outbox_count
