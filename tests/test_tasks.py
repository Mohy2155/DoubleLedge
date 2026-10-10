import pytest
from unittest.mock import patch
from workers.tasks import run_reconciliation_audit

@pytest.mark.django_db
@patch('workers.tasks.run_global_reconciliation')
def test_reconciliation_task_dispatch(mock_run_reconciliation):
    # Execute the celery task synchronously for testing
    run_reconciliation_audit.apply()
    
    # Assert the internal engine was actually called
    mock_run_reconciliation.assert_called_once()

import responses
from workers.tasks import dispatch_webhooks
from ledger.models import Tenant, WebhookEndpoint, OutboxEvent, WebhookDelivery
from django.utils import timezone
import socket

@pytest.mark.django_db(transaction=True)
@responses.activate
def test_dispatch_webhooks_end_to_end(monkeypatch):
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")

    tenant = Tenant.objects.create(name="Webhook Task Tenant")
    ep = WebhookEndpoint.objects.create(
        tenant=tenant, url="https://example.com/rcv", secret="test",
        event_types=["entry.posted"],
    )
    OutboxEvent.objects.create(
        tenant=tenant, type="entry.posted", payload={"id": "evt123"}
    )

    responses.add(
        responses.POST, "https://8.8.8.8/rcv",
        status=200, body="ok",
    )

    dispatch_webhooks.apply()

    deliveries = WebhookDelivery.objects.filter(endpoint=ep)
    assert deliveries.count() == 1

    delivery = deliveries.first()
    # C-4 fix verified: status must be DELIVERED (not stuck as PENDING due to fence bug)
    assert delivery.status == 'DELIVERED', (
        f"Expected DELIVERED but got {delivery.status} — possible C-4 fence regression"
    )
    assert delivery.delivered_at is not None
    assert delivery.last_status_code == 200

    req = responses.calls[0].request
    assert req.headers['Host'] == 'example.com'
    assert 'X-Webhook-Id' in req.headers
    assert 'X-Webhook-Timestamp' in req.headers
    assert 'X-Webhook-Signature' in req.headers


@pytest.mark.django_db(transaction=True)
@pytest.mark.runtime_role
@responses.activate
def test_dispatch_webhooks_under_runtime_role_multiple_tenants(monkeypatch):
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")

    # Created via create_tenant wrapper which resets role to postgres during creation
    tenant1 = Tenant.objects.create(name="Runtime Webhook Tenant 1")
    tenant2 = Tenant.objects.create(name="Runtime Webhook Tenant 2")

    # Set tenant context to insert endpoints and outbox events under RLS
    from django.db import connection
    with connection.cursor() as c:
        c.execute("SELECT set_config('app.tenant_id', %s, false);", [str(tenant1.id)])
    ep1 = WebhookEndpoint.objects.create(
        tenant=tenant1, url="https://example.com/rcv1", secret="secret1",
        event_types=["entry.posted"],
    )
    OutboxEvent.objects.create(
        tenant=tenant1, type="entry.posted", payload={"msg": "t1"}
    )

    with connection.cursor() as c:
        c.execute("SELECT set_config('app.tenant_id', %s, false);", [str(tenant2.id)])
    ep2 = WebhookEndpoint.objects.create(
        tenant=tenant2, url="https://example.com/rcv2", secret="secret2",
        event_types=["entry.posted"],
    )
    OutboxEvent.objects.create(
        tenant=tenant2, type="entry.posted", payload={"msg": "t2"}
    )

    # Clear tenant session context before dispatching to prove SECURITY DEFINER list_tenant_ids() works
    with connection.cursor() as c:
        c.execute("SELECT set_config('app.tenant_id', '', false);")

    responses.add(
        responses.POST, "https://8.8.8.8/rcv1",
        status=200, body="ok1",
    )
    responses.add(
        responses.POST, "https://8.8.8.8/rcv2",
        status=200, body="ok2",
    )

    dispatch_webhooks.apply()

    with connection.cursor() as c:
        c.execute("SELECT set_config('app.tenant_id', %s, false);", [str(tenant1.id)])
    d1 = WebhookDelivery.objects.filter(endpoint=ep1).first()
    assert d1 is not None
    assert d1.status == 'DELIVERED'
    assert d1.last_status_code == 200

    with connection.cursor() as c:
        c.execute("SELECT set_config('app.tenant_id', %s, false);", [str(tenant2.id)])
    d2 = WebhookDelivery.objects.filter(endpoint=ep2).first()
    assert d2 is not None
    assert d2.status == 'DELIVERED'
    assert d2.last_status_code == 200

