import uuid

from django.db import models
from django.utils import timezone


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

    class Meta:
        managed = False
        db_table = 'outbox_events'
