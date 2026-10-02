import pytest
import threading
import responses
from unittest.mock import patch

from django.db import connection, transaction
from django.utils import timezone

from ledger.models import Tenant, WebhookEndpoint, OutboxEvent, WebhookDelivery
from ledger.services.tenancy import for_each_tenant
from ledger.services.webhooks import (
    ClaimedDelivery,
    WebhookDispatchError,
    _assert_safe_ip,
    claim_deliveries,
    fan_out_events,
    generate_signature,
    prune_old_events_and_deliveries,
    record_delivery_result,
    send_webhook,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tenant():
    return Tenant.objects.create(name="Test Tenant")


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_fan_out_events(tenant):
    def run(t_id):
        ep1 = WebhookEndpoint.objects.create(
            tenant=tenant, url="https://a.com", secret="x", event_types=["entry.posted"]
        )
        ep2 = WebhookEndpoint.objects.create(
            tenant=tenant, url="https://b.com", secret="x",
            event_types=["entry.posted", "entry.reversed"]
        )
        WebhookEndpoint.objects.create(
            tenant=tenant, url="https://c.com", secret="x",
            event_types=["entry.posted"], is_active=False
        )

        evt_posted   = OutboxEvent.objects.create(tenant=tenant, type="entry.posted", payload={})
        evt_reversed = OutboxEvent.objects.create(tenant=tenant, type="entry.reversed", payload={})
        evt_other    = OutboxEvent.objects.create(tenant=tenant, type="other.event", payload={})

        count = fan_out_events()
        assert count == 3

        evt_posted.refresh_from_db()
        assert evt_posted.fanned_out_at is not None
        dels_posted = WebhookDelivery.objects.filter(event=evt_posted)
        assert dels_posted.count() == 2
        assert set(d.endpoint_id for d in dels_posted) == {ep1.id, ep2.id}

        evt_reversed.refresh_from_db()
        assert evt_reversed.fanned_out_at is not None
        dels_reversed = WebhookDelivery.objects.filter(event=evt_reversed)
        assert dels_reversed.count() == 1
        assert dels_reversed.first().endpoint_id == ep2.id

        # Zero-endpoint events are still marked fanned out
        evt_other.refresh_from_db()
        assert evt_other.fanned_out_at is not None
        assert WebhookDelivery.objects.filter(event=evt_other).count() == 0

    for_each_tenant(run)


@pytest.mark.django_db(transaction=True)
def test_fan_out_crash_leaves_unfanned(tenant):
    def run(t_id):
        WebhookEndpoint.objects.create(
            tenant=tenant, url="https://a.com", secret="x", event_types=["entry.posted"]
        )
        evt = OutboxEvent.objects.create(tenant=tenant, type="entry.posted", payload={})

        with patch('django.db.models.manager.BaseManager.bulk_create',
                   side_effect=Exception("Crash!")):
            with pytest.raises(Exception, match="Crash!"):
                fan_out_events()

        evt.refresh_from_db()
        assert evt.fanned_out_at is None

    for_each_tenant(run)


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_claim_deliveries_sweep_and_lease(tenant):
    def run(t_id):
        ep  = WebhookEndpoint.objects.create(
            tenant=tenant, url="https://a.com", secret="x", event_types=["entry.posted"]
        )
        evt = OutboxEvent.objects.create(tenant=tenant, type="entry.posted", payload={})

        # d1: ready
        d1 = WebhookDelivery.objects.create(
            tenant=tenant, event=evt, endpoint=ep, status='PENDING', attempts=0,
            next_attempt_at=timezone.now() - timezone.timedelta(minutes=1),
        )
        # d2: exhausted — should be swept to FAILED, not claimed
        d2 = WebhookDelivery.objects.create(
            tenant=tenant, event=evt, endpoint=ep, status='PENDING', attempts=8,
            next_attempt_at=timezone.now() - timezone.timedelta(minutes=1),
        )

        claimed = claim_deliveries(batch_size=10, lease_seconds=60)

        assert len(claimed) == 1
        assert claimed[0].delivery.id == d1.id
        # C-4: post-increment attempts_at_claim must equal 1 (was 0, +1)
        assert claimed[0].attempts_at_claim == 1

        d2.refresh_from_db()
        assert d2.status == 'FAILED'

        d1.refresh_from_db()
        assert d1.attempts == 1
        assert d1.next_attempt_at > timezone.now()

    for_each_tenant(run)


@pytest.mark.django_db(transaction=True)
def test_claim_deliveries_concurrency(tenant):
    ep = WebhookEndpoint.objects.create(
        tenant=tenant, url="https://a.com", secret="x", event_types=["entry.posted"]
    )
    for i in range(5):
        evt = OutboxEvent.objects.create(tenant=tenant, type="entry.posted", payload={"i": i})
        WebhookDelivery.objects.create(
            tenant=tenant, event=evt, endpoint=ep, status='PENDING', attempts=0,
            next_attempt_at=timezone.now() - timezone.timedelta(minutes=1),
        )

    claimed_lists = []

    def worker():
        connection.connect()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('app.tenant_id', %s, true)",
                        [str(tenant.id)],
                    )
                items = claim_deliveries(batch_size=3, lease_seconds=60)
                claimed_lists.append([c.delivery.id for c in items])
        finally:
            connection.close()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(claimed_lists) == 2
    lens = sorted(len(c) for c in claimed_lists)
    assert lens == [2, 3]
    assert set(claimed_lists[0]).isdisjoint(set(claimed_lists[1]))


@pytest.mark.django_db(transaction=True)
def test_claim_lease_expiry_redelivery(tenant):
    """T-2: a delivery whose lease has expired is re-claimed by a subsequent worker."""
    def run(t_id):
        ep  = WebhookEndpoint.objects.create(
            tenant=tenant, url="https://a.com", secret="x", event_types=["entry.posted"]
        )
        evt = OutboxEvent.objects.create(tenant=tenant, type="entry.posted", payload={})
        # Simulate a worker crash: the lease already expired (next_attempt_at in the past)
        d = WebhookDelivery.objects.create(
            tenant=tenant, event=evt, endpoint=ep, status='PENDING', attempts=1,
            next_attempt_at=timezone.now() - timezone.timedelta(seconds=1),
        )

        claimed = claim_deliveries(batch_size=10, lease_seconds=60)

        assert len(claimed) == 1
        assert claimed[0].delivery.id == d.id
        assert claimed[0].attempts_at_claim == 2   # incremented again

    for_each_tenant(run)


# ---------------------------------------------------------------------------
# SSRF / IP block-list
# ---------------------------------------------------------------------------

def test_assert_safe_ip_blocks_loopback():
    with pytest.raises(WebhookDispatchError, match="Blocked IP"):
        _assert_safe_ip("127.0.0.1")

def test_assert_safe_ip_blocks_ipv6_loopback():
    with pytest.raises(WebhookDispatchError, match="Blocked IP"):
        _assert_safe_ip("::1")

def test_assert_safe_ip_blocks_private_rfc1918():
    for ip in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
        with pytest.raises(WebhookDispatchError, match="Blocked IP"):
            _assert_safe_ip(ip)

def test_assert_safe_ip_blocks_link_local_metadata():
    with pytest.raises(WebhookDispatchError, match="Blocked IP"):
        _assert_safe_ip("169.254.169.254")   # AWS/GCP metadata

def test_assert_safe_ip_blocks_ipv6_link_local():
    with pytest.raises(WebhookDispatchError, match="Blocked IP"):
        _assert_safe_ip("fe80::1")

def test_assert_safe_ip_blocks_multicast():
    with pytest.raises(WebhookDispatchError, match="Blocked IP"):
        _assert_safe_ip("224.0.0.1")

def test_assert_safe_ip_allows_public():
    _assert_safe_ip("8.8.8.8")    # should not raise


@pytest.mark.django_db
def test_send_webhook_ssrf_blocked_ips(tenant, monkeypatch):
    import socket as _socket
    ep = WebhookEndpoint.objects.create(
        tenant=tenant, url="https://example.com", secret="x", event_types=["entry.posted"]
    )
    evt = OutboxEvent.objects.create(tenant=tenant, type="entry.posted", payload={})
    delivery = WebhookDelivery.objects.create(
        tenant=tenant, event=evt, endpoint=ep, status='PENDING', attempts=0,
        next_attempt_at=timezone.now(),
    )

    for ip in ("127.0.0.1", "192.168.1.1", "169.254.169.254", "224.0.0.1", "10.0.0.1", "::1"):
        monkeypatch.setattr(_socket, "gethostbyname", lambda h, _ip=ip: _ip)
        with pytest.raises(WebhookDispatchError, match="Blocked IP"):
            send_webhook(delivery)


@pytest.mark.django_db
@responses.activate
def test_send_webhook_no_redirects(tenant, monkeypatch):
    import socket as _socket
    monkeypatch.setattr(_socket, "gethostbyname", lambda h: "8.8.8.8")

    ep = WebhookEndpoint.objects.create(
        tenant=tenant, url="https://example.com/webhooks", secret="x",
        event_types=["entry.posted"]
    )
    evt = OutboxEvent.objects.create(tenant=tenant, type="entry.posted", payload={})
    delivery = WebhookDelivery.objects.create(
        tenant=tenant, event=evt, endpoint=ep, status='PENDING', attempts=0,
        next_attempt_at=timezone.now(),
    )

    responses.add(
        responses.POST, "https://8.8.8.8/webhooks",
        status=301, headers={"Location": "https://other.com"},
    )

    resp = send_webhook(delivery)
    assert resp.status_code == 301  # redirect not followed


# ---------------------------------------------------------------------------
# HMAC signature
# ---------------------------------------------------------------------------

def test_generate_signature_fixed_vector():
    # Fixed vector: echo -n "1672531200.{\"id\":\"test\"}" | python -c "
    #   import hmac, hashlib, sys
    #   print(hmac.new(b'whsec_test123', sys.stdin.buffer.read(), hashlib.sha256).hexdigest())"
    sig = generate_signature("whsec_test123", "1672531200", '{"id":"test"}')
    assert sig == "v1=c3a05a571c3b8605dfc4beaba6f06e9f5f54acf7cca6c5d92f31196aff737593"


# ---------------------------------------------------------------------------
# Result recording  (uses ClaimedDelivery — C-4 fix)
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_record_delivery_result(tenant):
    def run(t_id):
        ep  = WebhookEndpoint.objects.create(
            tenant=tenant, url="https://a.com", secret="x", event_types=["entry.posted"]
        )
        evt = OutboxEvent.objects.create(tenant=tenant, type="entry.posted", payload={})

        # 1. Success
        d_succ = WebhookDelivery.objects.create(
            tenant=tenant, event=evt, endpoint=ep, status='PENDING', attempts=1
        )
        record_delivery_result(
            ClaimedDelivery(delivery=d_succ, attempts_at_claim=1),
            success=True, status_code=200,
        )
        d_succ.refresh_from_db()
        assert d_succ.status == 'DELIVERED'
        assert d_succ.delivered_at is not None

        # 2. Retry scheduling
        d_retry = WebhookDelivery.objects.create(
            tenant=tenant, event=evt, endpoint=ep, status='PENDING', attempts=2
        )
        record_delivery_result(
            ClaimedDelivery(delivery=d_retry, attempts_at_claim=2),
            success=False, status_code=500, error="Timeout",
        )
        d_retry.refresh_from_db()
        assert d_retry.status == 'PENDING'
        assert d_retry.last_status_code == 500
        assert d_retry.last_error == "Timeout"
        assert d_retry.next_attempt_at > timezone.now()

        # 3. Terminal failure
        d_fail = WebhookDelivery.objects.create(
            tenant=tenant, event=evt, endpoint=ep, status='PENDING', attempts=8
        )
        record_delivery_result(
            ClaimedDelivery(delivery=d_fail, attempts_at_claim=8),
            success=False, status_code=400,
        )
        d_fail.refresh_from_db()
        assert d_fail.status == 'FAILED'

        # 4. Stale result rejection (fence) — DB row advanced past claim snapshot
        d_stale = WebhookDelivery.objects.create(
            tenant=tenant, event=evt, endpoint=ep, status='PENDING', attempts=3
        )
        # Attempt to record with stale claim (attempts_at_claim=2, but DB is at 3)
        record_delivery_result(
            ClaimedDelivery(delivery=d_stale, attempts_at_claim=2),
            success=True, status_code=200,
        )
        d_stale.refresh_from_db()
        assert d_stale.status == 'PENDING'   # rejected — stale

        # 5. One failing endpoint does not block another
        ep2 = WebhookEndpoint.objects.create(
            tenant=tenant, url="https://b.com", secret="x", event_types=["entry.posted"]
        )
        evt2 = OutboxEvent.objects.create(tenant=tenant, type="entry.posted", payload={})
        d1 = WebhookDelivery.objects.create(
            tenant=tenant, event=evt2, endpoint=ep, status='PENDING', attempts=1
        )
        d2 = WebhookDelivery.objects.create(
            tenant=tenant, event=evt2, endpoint=ep2, status='PENDING', attempts=1
        )
        record_delivery_result(
            ClaimedDelivery(delivery=d1, attempts_at_claim=1),
            success=False, error="Err",
        )
        record_delivery_result(
            ClaimedDelivery(delivery=d2, attempts_at_claim=1),
            success=True, status_code=201,
        )
        d1.refresh_from_db()
        d2.refresh_from_db()
        assert d1.status == 'PENDING'
        assert d2.status == 'DELIVERED'

    for_each_tenant(run)


# ---------------------------------------------------------------------------
# Cross-tenant delivery isolation  (T-5)
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_fan_out_cross_tenant_isolation():
    """
    Fan-out must only create deliveries for tenant A's endpoints.
    Tenant B's endpoints must receive no deliveries for tenant A's events.

    Note: The test DB user is postgres (BYPASSRLS), so RLS row filtering
    is not enforced on raw .count() queries.  We assert isolation by checking
    tenant_id on the delivery rows directly.
    """
    tenant_a = Tenant.objects.create(name="Tenant A")
    tenant_b = Tenant.objects.create(name="Tenant B")

    # Tenant B has an endpoint for the same event type
    ep_b = WebhookEndpoint.objects.create(
        tenant=tenant_b, url="https://b.com", secret="x", event_types=["entry.posted"]
    )

    with transaction.atomic():
        with connection.cursor() as c:
            c.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_a.id)])
        ep_a = WebhookEndpoint.objects.create(
            tenant=tenant_a, url="https://a.com", secret="x", event_types=["entry.posted"]
        )
        OutboxEvent.objects.create(tenant=tenant_a, type="entry.posted", payload={})
        fan_out_events()

    # Only tenant A's endpoint should have received a delivery
    assert WebhookDelivery.objects.filter(endpoint=ep_a).count() == 1
    # Tenant B's endpoint must NOT have received a delivery for tenant A's event
    assert WebhookDelivery.objects.filter(endpoint=ep_b).count() == 0







# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_pruning(tenant):
    def run(t_id):
        old    = timezone.now() - timezone.timedelta(days=10)
        recent = timezone.now() - timezone.timedelta(days=1)

        ep = WebhookEndpoint.objects.create(
            tenant=tenant, url="https://a.com", secret="x", event_types=["entry.posted"]
        )

        # 1. Old event + DELIVERED delivery → should be deleted
        evt_old1 = OutboxEvent.objects.create(
            tenant=tenant, type="entry.posted", payload={}, fanned_out_at=old
        )
        evt_old1.created_at = old
        evt_old1.save()
        del_old1 = WebhookDelivery.objects.create(
            tenant=tenant, event=evt_old1, endpoint=ep, status='DELIVERED', attempts=1
        )
        del_old1.created_at = old
        del_old1.save()

        # 2. Old event + PENDING delivery → must NOT be deleted
        evt_old2 = OutboxEvent.objects.create(
            tenant=tenant, type="entry.posted", payload={}, fanned_out_at=old
        )
        evt_old2.created_at = old
        evt_old2.save()
        del_old2 = WebhookDelivery.objects.create(
            tenant=tenant, event=evt_old2, endpoint=ep, status='PENDING', attempts=1
        )
        del_old2.created_at = old
        del_old2.save()

        # 3. Recent event + DELIVERED delivery → must NOT be deleted
        evt_rec = OutboxEvent.objects.create(
            tenant=tenant, type="entry.posted", payload={}, fanned_out_at=recent
        )
        evt_rec.created_at = recent
        evt_rec.save()
        del_rec = WebhookDelivery.objects.create(
            tenant=tenant, event=evt_rec, endpoint=ep, status='DELIVERED', attempts=1
        )
        del_rec.created_at = recent
        del_rec.save()

        # 4. Old event never fanned out → must NOT be deleted
        evt_unfanned = OutboxEvent.objects.create(tenant=tenant, type="entry.posted", payload={})
        evt_unfanned.created_at = old
        evt_unfanned.save()

        prune_old_events_and_deliveries(days=7)

        assert not WebhookDelivery.objects.filter(id=del_old1.id).exists()
        assert not OutboxEvent.objects.filter(id=evt_old1.id).exists()

        assert WebhookDelivery.objects.filter(id=del_old2.id).exists()
        assert OutboxEvent.objects.filter(id=evt_old2.id).exists()

        assert WebhookDelivery.objects.filter(id=del_rec.id).exists()
        assert OutboxEvent.objects.filter(id=evt_rec.id).exists()

        assert OutboxEvent.objects.filter(id=evt_unfanned.id).exists()

    for_each_tenant(run)
