import datetime

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from ledger.models import IdempotencyKey, PeriodClose, Tenant
from ledger.services.idempotency import get_or_insert_idempotency_key


class PeriodCloseError(Exception):
    pass


def iso_utc(value):
    return value.astimezone(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def get_cutoff(tenant_id):
    """The tenant's current cutoff (entries with effective_at < cutoff are frozen), or None."""
    return PeriodClose.objects.filter(tenant_id=tenant_id).aggregate(cutoff=Max('cutoff'))['cutoff']


def close_period(tenant, cutoff, source=PeriodClose.Source.MANUAL):
    """
    Records a close. Caller owns the transaction and tenant context.
    These checks only produce friendly errors; trg_period_closes_valid is authoritative
    and also serializes the close against in-flight postings.
    """
    if cutoff > timezone.now():
        raise PeriodCloseError("invalid_period_cutoff")
    current = get_cutoff(tenant.id)
    if current is not None and cutoff <= current:
        raise PeriodCloseError("invalid_period_cutoff")
    try:
        return PeriodClose.objects.create(tenant=tenant, cutoff=cutoff, source=source)
    except IntegrityError as exc:
        # A concurrent close won the race after our pre-check.
        if str(exc).startswith("invalid_period_cutoff"):
            raise PeriodCloseError("invalid_period_cutoff") from exc
        raise


@transaction.atomic
def request_close(tenant, cutoff, idempotency_key: str, request_hash: bytes):
    """Manual close via the API. Returns (status_code, body, is_replayed)."""
    key_row, created = get_or_insert_idempotency_key(tenant.id, idempotency_key, request_hash)
    if not created:
        if bytes(key_row.request_hash) != request_hash:
            raise PeriodCloseError("idempotency_key_reuse")
        return key_row.response_code, key_row.response_body, True

    close = close_period(tenant, cutoff)
    body = {
        "id": str(close.id),
        "cutoff": iso_utc(close.cutoff),
        "source": close.source,
        "created_at": iso_utc(close.created_at),
    }
    IdempotencyKey.objects.filter(tenant_id=tenant.id, key=idempotency_key).update(
        response_code=201, response_body=body
    )
    return 201, body, False


def scheduled_cutoff(now, grace_days):
    """
    Start of the latest UTC month M whose previous month is eligible to close,
    i.e. now >= M + grace_days. grace_days <= 27 means stepping back one month always suffices.
    """
    month_start = now.astimezone(datetime.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now < month_start + datetime.timedelta(days=grace_days):
        month_start = (month_start - datetime.timedelta(days=1)).replace(day=1)
    return month_start


def close_due_period(tenant_id, now=None):
    """Scheduled close for one tenant (caller sets the tenant context). Returns the new close or None."""
    tenant = Tenant.objects.get(id=tenant_id)
    if tenant.period_close_grace_days is None:
        return None
    cutoff = scheduled_cutoff(now or timezone.now(), tenant.period_close_grace_days)
    current = get_cutoff(tenant_id)
    if current is not None and cutoff <= current:
        return None
    return close_period(tenant, cutoff, PeriodClose.Source.SCHEDULED)
