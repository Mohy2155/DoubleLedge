import concurrent.futures

import pytest
from django.db import transaction

from ledger.models import IdempotencyKey, Tenant
from ledger.services.idempotency import get_or_insert_idempotency_key


@pytest.fixture
def tenant():
    return Tenant.objects.create(name="Idempotency Tenant")


@pytest.mark.django_db(transaction=True)
def test_idempotency_key_duplicate_returns_existing(tenant):
    req_hash = b"abc"
    
    # First insert
    key1, created1 = get_or_insert_idempotency_key(tenant.id, "my_key", req_hash)
    assert created1 is True
    
    # Update the row to simulate finishing a request
    key1.response_code = 200
    key1.response_body = {"status": "ok"}
    key1.save()
    
    # Second insert
    key2, created2 = get_or_insert_idempotency_key(tenant.id, "my_key", req_hash)
    assert created2 is False
    assert key2.response_code == 200
    assert key2.response_body == {"status": "ok"}


@pytest.mark.django_db(transaction=True)
def test_idempotency_concurrent_threads(tenant):
    req_hash = b"xyz"
    key_name = "concurrent_key"
    
    def worker():
        # Django thread DB connection issue workaround
        from django.db import connection
        connection.close()
        
        with transaction.atomic():
            key, created = get_or_insert_idempotency_key(tenant.id, key_name, req_hash)
            if created:
                key.response_code = 201
                key.save()
            return created

    # Fire 10 threads at once
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(lambda _: worker(), range(10)))
    
    # Exactly one thread should have 'created = True'
    assert results.count(True) == 1
    assert results.count(False) == 9
    
    # The row should exist
    assert IdempotencyKey.objects.filter(tenant_id=tenant.id, key=key_name).count() == 1
    
