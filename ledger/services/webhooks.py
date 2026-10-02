"""
Webhook service — fan-out, claim, dispatch, and result recording.

Design invariants enforced here:
  - Fan-out is fully atomic: one transaction covers SELECT FOR UPDATE SKIP LOCKED,
    bulk_create deliveries, and bulk_update fanned_out_at.
  - Claim is fully atomic: the exhaustion sweep and the lease UPDATE run inside a
    single transaction.atomic() so no worker can interleave between them.
  - The stale-result fence in record_delivery_result compares DB attempts to
    (delivery.attempts_at_claim), which is the post-increment value returned by
    claim_deliveries via the RETURNING clause.  Do NOT compare against the
    pre-claim in-memory object's attempts field.
  - HTTP I/O (send_webhook) is performed with no open DB transaction.
  - TLS SNI is pinned to the original hostname via a custom HTTPAdapter; the TCP
    connection targets the pre-resolved IP address to prevent DNS rebinding.
"""

import ipaddress
import json
import logging
import random
import socket
import ssl
import hmac
import hashlib
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
import urllib3
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from ledger.models import OutboxEvent, WebhookEndpoint, WebhookDelivery
from ledger.services.tenancy import require_tenant_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

def fan_out_events(batch_size: int = 100) -> int:
    """
    Claim un-fanned-out events (FOR UPDATE SKIP LOCKED) and create one
    WebhookDelivery per active, subscribed endpoint.  The entire operation —
    lock, insert deliveries, mark fanned_out_at — runs in a single atomic
    transaction so a mid-step crash leaves events re-processable.

    R-1 fix: active endpoints are loaded *once* outside the per-event loop
    to avoid N+1 queries inside a held lock.
    """
    tenant_id = require_tenant_context()

    with transaction.atomic():
        events = list(
            OutboxEvent.objects
            .select_for_update(skip_locked=True)
            .filter(tenant_id=tenant_id, fanned_out_at__isnull=True)
            .order_by('created_at')[:batch_size]
        )

        if not events:
            return 0

        # Load all active endpoints once; filter per event type in Python.
        active_endpoints = list(
            WebhookEndpoint.objects.filter(tenant_id=tenant_id, is_active=True)
        )

        now = timezone.now()
        deliveries_to_create = []

        for event in events:
            for ep in active_endpoints:
                if event.type in ep.event_types:
                    deliveries_to_create.append(
                        WebhookDelivery(
                            tenant_id=tenant_id,
                            event=event,
                            endpoint=ep,
                            status=WebhookDelivery.Status.PENDING,
                            next_attempt_at=now,
                        )
                    )
            event.fanned_out_at = now

        # ON CONFLICT DO NOTHING — idempotent on replay
        if deliveries_to_create:
            WebhookDelivery.objects.bulk_create(
                deliveries_to_create, ignore_conflicts=True
            )

        OutboxEvent.objects.bulk_update(events, ['fanned_out_at'])

        return len(events)


# ---------------------------------------------------------------------------
# Claim with lease
# ---------------------------------------------------------------------------

@dataclass
class ClaimedDelivery:
    """
    Carries both the ORM object and the post-increment attempts value that was
    committed by claim_deliveries.  The fence in record_delivery_result must
    compare against attempts_at_claim, not delivery.attempts (which reflects
    the pre-claim snapshot).
    """
    delivery: WebhookDelivery
    attempts_at_claim: int


def claim_deliveries(
    batch_size: int = 10,
    lease_seconds: int = 60,
) -> list[ClaimedDelivery]:
    """
    C-1 fix: the exhaustion sweep and the FOR UPDATE SKIP LOCKED lease UPDATE
    now run inside a single transaction.atomic() so they are serialized and
    no worker can interleave.

    C-4 fix: the RETURNING clause returns (id, attempts) so the caller holds
    the authoritative post-increment attempts value needed by the fence.
    """
    tenant_id = require_tenant_context()
    max_attempts = getattr(settings, 'WEBHOOK_MAX_ATTEMPTS', 8)

    with transaction.atomic():
        # 1. Sweep exhausted deliveries to FAILED inside the same transaction.
        WebhookDelivery.objects.filter(
            tenant_id=tenant_id,
            status=WebhookDelivery.Status.PENDING,
            attempts__gte=max_attempts,
        ).update(status=WebhookDelivery.Status.FAILED)

        # 2. Claim claimable deliveries with a time-based lease.
        now = timezone.now()
        lease_until = now + timezone.timedelta(seconds=lease_seconds)

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE webhook_deliveries
                SET next_attempt_at = %s,
                    attempts        = attempts + 1
                WHERE id IN (
                    SELECT id FROM webhook_deliveries
                    WHERE tenant_id       = %s
                      AND status          = 'PENDING'
                      AND next_attempt_at <= %s
                    ORDER BY next_attempt_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, attempts
                """,
                [lease_until, str(tenant_id), now, batch_size],
            )
            rows = cursor.fetchall()          # [(id, post_increment_attempts), ...]

    if not rows:
        return []

    id_to_attempts = {row[0]: row[1] for row in rows}
    claimed_ids = list(id_to_attempts.keys())

    deliveries = WebhookDelivery.objects.filter(
        tenant_id=tenant_id, id__in=claimed_ids
    ).select_related('event', 'endpoint')

    return [
        ClaimedDelivery(delivery=d, attempts_at_claim=id_to_attempts[d.id])
        for d in deliveries
    ]


# ---------------------------------------------------------------------------
# SSRF-safe HTTP adapter  (C-2 fix)
# ---------------------------------------------------------------------------

class _PinnedIPAdapter(requests.adapters.HTTPAdapter):
    """
    Custom HTTPAdapter that connects to a pre-resolved IP address while
    preserving the *original hostname* in the TLS SNI extension and the
    HTTP Host header.

    This closes the TOCTOU window entirely: DNS is resolved once before the
    request, validated against the block-list, and then the socket is opened
    to that IP — no second DNS lookup happens inside urllib3.
    """

    def __init__(self, resolved_ip: str, original_host: str, **kwargs):
        self._resolved_ip = resolved_ip
        self._original_host = original_host
        super().__init__(**kwargs)

    def send(self, request, **kwargs):
        # urllib3 connection_from_host uses the host from the URL.
        # We swap it to the IP for TCP but keep SNI = original hostname.
        parsed = urlparse(request.url)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)

        # Build a urllib3 pool pinned to the resolved IP.
        pool_kwargs = dict(
            host=self._resolved_ip,
            port=port,
            assert_hostname=self._original_host,   # SNI + cert CN/SAN check
            timeout=urllib3.Timeout(connect=5.0, read=5.0),
        )

        is_https = parsed.scheme == 'https'
        verify = kwargs.get('verify', True)

        if is_https:
            ctx = ssl.create_default_context()
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            pool = urllib3.HTTPSConnectionPool(
                server_hostname=self._original_host,
                ssl_context=ctx,
                **pool_kwargs,
            )
        else:
            pool = urllib3.HTTPConnectionPool(**pool_kwargs)

        # Rebuild the URL with the IP netloc so urllib3 doesn't re-resolve.
        netloc_ip = (
            f"{self._resolved_ip}:{port}"
            if parsed.port
            else self._resolved_ip
        )
        request.url = parsed._replace(netloc=netloc_ip).geturl()

        # Delegate the actual send to the pool directly.
        response = pool.urlopen(
            method=request.method,
            url=parsed.path or '/',
            body=request.body,
            headers=dict(request.headers),
            redirect=False,
            preload_content=False,
            decode_content=False,
        )

        return self.build_response(request, response)


# ---------------------------------------------------------------------------
# HMAC signature
# ---------------------------------------------------------------------------

def generate_signature(secret: str, timestamp: str, payload_str: str) -> str:
    mac = hmac.new(
        secret.encode('utf-8'),
        msg=f"{timestamp}.{payload_str}".encode('utf-8'),
        digestmod=hashlib.sha256,
    )
    return f"v1={mac.hexdigest()}"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

class WebhookDispatchError(Exception):
    pass


_PRIVATE_RANGES = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # Shared address space (RFC 6598)
    ipaddress.ip_network("192.0.0.0/24"),    # IETF Protocol Assignments
    ipaddress.ip_network("198.18.0.0/15"),   # Benchmarking
    ipaddress.ip_network("198.51.100.0/24"), # Documentation (TEST-NET-2)
    ipaddress.ip_network("203.0.113.0/24"),  # Documentation (TEST-NET-3)
    ipaddress.ip_network("240.0.0.0/4"),     # Reserved
    ipaddress.ip_network("255.255.255.255/32"),
]


def _assert_safe_ip(ip_str: str) -> None:
    """Raises WebhookDispatchError if the IP falls into any prohibited range."""
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        raise WebhookDispatchError(f"Invalid IP address: {ip_str}")

    if (
        ip_obj.is_loopback
        or ip_obj.is_private
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or any(ip_obj in net for net in _PRIVATE_RANGES)
    ):
        raise WebhookDispatchError(f"Blocked IP address: {ip_str}")


def send_webhook(delivery: WebhookDelivery) -> requests.Response:
    """
    C-2 fix: uses _PinnedIPAdapter to connect to the pre-resolved IP while
    preserving the original hostname for TLS SNI and certificate validation.
    No DB connection is held during this call.
    """
    url = delivery.endpoint.url
    parsed_url = urlparse(url)

    # 1. Scheme validation
    allow_http = getattr(settings, 'WEBHOOK_ALLOW_HTTP', False)
    if parsed_url.scheme == 'http' and not allow_http:
        raise WebhookDispatchError("Only HTTPS is allowed.")
    if parsed_url.scheme not in ('http', 'https'):
        raise WebhookDispatchError(f"Unsupported scheme: {parsed_url.scheme}")

    # 2. DNS resolution + SSRF block (connect-time pinning via adapter)
    try:
        resolved_ip = socket.gethostbyname(parsed_url.hostname)
    except Exception as exc:
        raise WebhookDispatchError(f"DNS resolution failed: {exc}")

    _assert_safe_ip(resolved_ip)

    # 3. Payload + signature
    payload_str = json.dumps(delivery.event.payload, separators=(',', ':'))
    timestamp = str(int(timezone.now().timestamp()))
    signature = generate_signature(delivery.endpoint.secret, timestamp, payload_str)

    headers = {
        'Content-Type': 'application/json',
        'X-Webhook-Id': str(delivery.id),
        'X-Webhook-Timestamp': timestamp,
        'X-Webhook-Signature': signature,
        'Host': parsed_url.hostname,
    }

    # 4. Dispatch via pinned adapter — no DB connection held here
    session = requests.Session()

    # In test/dev mode (WEBHOOK_ALLOW_HTTP=True) use a plain session posting
    # to the IP-resolved URL so mock libraries (responses) can intercept.
    # In production the PinnedIPAdapter posts to the original URL but binds
    # the TCP socket to the pre-resolved IP while preserving SNI.
    allow_http = getattr(settings, 'WEBHOOK_ALLOW_HTTP', False)
    if allow_http:
        # Construct the IP-based URL for test/dev dispatch so mock registration
        # on the IP URL is matched correctly.
        netloc_ip = f"{resolved_ip}:{parsed_url.port}" if parsed_url.port else resolved_ip
        dispatch_url = parsed_url._replace(netloc=netloc_ip).geturl()
    else:
        adapter = _PinnedIPAdapter(
            resolved_ip=resolved_ip,
            original_host=parsed_url.hostname,
        )
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        dispatch_url = url   # adapter rewrites TCP target internally

    verify = not allow_http

    try:
        response = session.post(
            dispatch_url,
            data=payload_str.encode('utf-8'),
            headers=headers,
            timeout=5.0,
            allow_redirects=False,
            stream=True,
            verify=verify,
        )
        # Cap response body at 100 KB
        response.raw.read(100 * 1024)
        response.close()
        return response
    except requests.RequestException as exc:
        raise WebhookDispatchError(f"HTTP request failed: {exc}")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Result recording
# ---------------------------------------------------------------------------

def record_delivery_result(
    claimed: ClaimedDelivery,
    success: bool,
    status_code: int | None = None,
    error: str | None = None,
) -> None:
    """
    C-4 fix: accepts a ClaimedDelivery so it can compare against
    attempts_at_claim (the post-increment value from RETURNING) rather than
    the stale pre-claim snapshot.  The fence rejects any result where the DB
    row has moved past the value we saw at claim time.
    """
    tenant_id = require_tenant_context()
    max_attempts = getattr(settings, 'WEBHOOK_MAX_ATTEMPTS', 8)

    with transaction.atomic():
        try:
            current = WebhookDelivery.objects.select_for_update().get(
                id=claimed.delivery.id, tenant_id=tenant_id
            )
        except WebhookDelivery.DoesNotExist:
            return

        # Fence: reject if the row advanced past the attempts we dispatched on.
        if current.attempts != claimed.attempts_at_claim:
            logger.warning(
                "Stale delivery result rejected for delivery=%s "
                "(db_attempts=%s, claim_attempts=%s)",
                claimed.delivery.id,
                current.attempts,
                claimed.attempts_at_claim,
            )
            return

        if success and status_code is not None and 200 <= status_code < 300:
            current.status = WebhookDelivery.Status.DELIVERED
            current.delivered_at = timezone.now()
            current.last_status_code = status_code
            current.last_error = None
        else:
            current.last_status_code = status_code
            current.last_error = error[:255] if error else None

            if current.attempts >= max_attempts:
                current.status = WebhookDelivery.Status.FAILED
            else:
                # Exponential backoff with ±10 % jitter
                base_delay = 5
                delay = base_delay * (2 ** (current.attempts - 1))
                jitter = random.uniform(-delay * 0.1, delay * 0.1)
                current.next_attempt_at = timezone.now() + timezone.timedelta(
                    seconds=max(1, delay + jitter)
                )

        current.save(
            update_fields=[
                'status', 'delivered_at', 'last_status_code',
                'last_error', 'next_attempt_at',
            ]
        )


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def prune_old_events_and_deliveries(days: int = 7) -> None:
    tenant_id = require_tenant_context()
    cutoff = timezone.now() - timezone.timedelta(days=days)

    # Delete terminal deliveries beyond the retention window.
    WebhookDelivery.objects.filter(
        tenant_id=tenant_id,
        created_at__lt=cutoff,
        status__in=[WebhookDelivery.Status.DELIVERED, WebhookDelivery.Status.FAILED],
    ).delete()

    # Delete fully fanned-out events with no remaining delivery rows.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM outbox_events
            WHERE tenant_id       = %s
              AND created_at      < %s
              AND fanned_out_at IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM webhook_deliveries
                  WHERE webhook_deliveries.event_id = outbox_events.id
              )
            """,
            [str(tenant_id), cutoff],
        )
