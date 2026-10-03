import uuid

from django.db import models
from django.utils import timezone
from django.contrib.postgres.fields import ArrayField


class Tenant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.TextField()

    class Meta:
        managed = False
        db_table = 'tenants'


class Account(models.Model):
    class Type(models.TextChoices):
        ASSET = 'ASSET'
        LIABILITY = 'LIABILITY'
        EQUITY = 'EQUITY'
        REVENUE = 'REVENUE'
        EXPENSE = 'EXPENSE'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.TextField()
    type = models.TextField(choices=Type.choices)
    currency = models.CharField(max_length=3)
    balance = models.DecimalField(max_digits=19, decimal_places=4, default=0)
    allow_negative = models.BooleanField(default=False)
    version = models.BigIntegerField(default=0)
    shard_count = models.IntegerField(default=0)
    parent_account = models.ForeignKey(
        'self', null=True, on_delete=models.PROTECT, related_name='shards', db_column='parent_account_id'
    )

    class Meta:
        managed = False
        db_table = 'accounts'
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'id'], name='unique_tenant_account')
        ]


class JournalEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    reference_id = models.TextField()
    description = models.TextField()
    reverses = models.OneToOneField('self', null=True, on_delete=models.PROTECT, related_name='reversed_by')
    effective_at = models.DateTimeField(default=timezone.now)
    posted_at = models.DateTimeField(default=timezone.now)
    metadata = models.JSONField(default=dict)

    class Meta:
        managed = False
        db_table = 'journal_entries'
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'reference_id'], name='unique_tenant_reference'),
            models.UniqueConstraint(fields=['tenant', 'id'], name='unique_tenant_journal_entry')
        ]


class TransactionLine(models.Model):
    class Direction(models.TextChoices):
        DEBIT = 'DEBIT'
        CREDIT = 'CREDIT'

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    journal_entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name='lines')
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    direction = models.TextField(choices=Direction.choices)
    amount = models.DecimalField(max_digits=19, decimal_places=4)

    class Meta:
        managed = False
        db_table = 'transaction_lines'


class IdempotencyKey(models.Model):
    # Django < 5.1 doesn't support composite PKs natively. 
    # We use 'key' as the PK for ORM convenience, but the DB uses (tenant_id, key).
    key = models.TextField(primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    request_hash = models.BinaryField()
    response_code = models.IntegerField(null=True)
    response_body = models.JSONField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        managed = False
        db_table = 'idempotency_keys'
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'key'], name='unique_tenant_key')
        ]


class OutboxEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    type = models.TextField()
    payload = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)
    delivered_at = models.DateTimeField(null=True)
    attempts = models.IntegerField(default=0)
    fanned_out_at = models.DateTimeField(null=True)

    class Meta:
        managed = False
        db_table = 'outbox_events'
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'id'], name='unique_tenant_outbox_event')
        ]


class WebhookEndpoint(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    url = models.TextField()
    secret = models.TextField()
    event_types = ArrayField(models.TextField())
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        managed = False
        db_table = 'webhook_endpoints'
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'id'], name='unique_tenant_endpoint_id'),
            models.UniqueConstraint(fields=['tenant', 'url'], name='unique_tenant_endpoint_url')
        ]


class WebhookDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING'
        DELIVERED = 'DELIVERED'
        FAILED = 'FAILED'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    event = models.ForeignKey(OutboxEvent, on_delete=models.CASCADE)
    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE)
    status = models.TextField(choices=Status.choices, default=Status.PENDING)
    attempts = models.IntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(null=True)
    last_status_code = models.IntegerField(null=True)
    delivered_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        managed = False
        db_table = 'webhook_deliveries'
        constraints = [
            models.UniqueConstraint(fields=['event', 'endpoint'], name='unique_event_endpoint')
        ]

class ReconciliationReport(models.Model):
    class Status(models.TextChoices):
        CLEAN = 'CLEAN'
        DRIFT_DETECTED = 'DRIFT_DETECTED'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    timestamp = models.DateTimeField(default=timezone.now)
    status = models.TextField(choices=Status.choices)
    accounts_checked = models.IntegerField(default=0)
    discrepancies = models.JSONField(default=dict)

    class Meta:
        managed = False
        db_table = 'reconciliation_reports'

class BalanceSnapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    sequence = models.BigIntegerField()
    entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT)
    balance = models.DecimalField(max_digits=28, decimal_places=4)
    debit_total = models.DecimalField(max_digits=28, decimal_places=4)
    credit_total = models.DecimalField(max_digits=28, decimal_places=4)
    line_count = models.BigIntegerField()
    max_posted_at = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        managed = False
        db_table = 'balance_snapshots'
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'account', 'sequence'], name='unique_tenant_account_sequence')
        ]

class Hold(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING'
        CAPTURED = 'CAPTURED'
        VOIDED = 'VOIDED'
        EXPIRED = 'EXPIRED'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    reference_id = models.TextField()
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='holds')
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=19, decimal_places=4)
    captured_amount = models.DecimalField(max_digits=19, decimal_places=4, default=0)
    status = models.TextField(choices=Status.choices, default=Status.PENDING)
    description = models.TextField(default='')
    journal_entry = models.ForeignKey(
        JournalEntry, null=True, on_delete=models.PROTECT, related_name='hold'
    )
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    metadata = models.JSONField(default=dict)

    class Meta:
        managed = False
        db_table = 'holds'
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'id'], name='unique_tenant_hold'),
            models.UniqueConstraint(fields=['tenant', 'reference_id'], name='unique_tenant_hold_reference'),
        ]
