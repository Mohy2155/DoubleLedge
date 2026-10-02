from rest_framework import serializers


class StrictStringDecimalField(serializers.DecimalField):
    """
    A Decimal field that ONLY accepts JSON strings, never floats or ints,
    preventing any precision loss before deserialization.
    """
    def to_internal_value(self, data):
        if not isinstance(data, str):
            raise serializers.ValidationError("Monetary amounts must be provided strictly as strings.")
        super().to_internal_value(data)
        return data

class TransactionLineSerializer(serializers.Serializer):
    account_id = serializers.UUIDField()
    amount = StrictStringDecimalField(max_digits=19, decimal_places=4)
    direction = serializers.ChoiceField(choices=['DEBIT', 'CREDIT'])

class JournalEntryCreateSerializer(serializers.Serializer):
    reference_id = serializers.CharField(max_length=255)
    description = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    lines = TransactionLineSerializer(many=True, allow_empty=False)

class JournalEntrySerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    reference_id = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    lines = TransactionLineSerializer(many=True, read_only=True)

class JournalEntryReverseSerializer(serializers.Serializer):
    reference_id = serializers.CharField(max_length=255, required=False)

class AccountSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    type = serializers.CharField(read_only=True)
    balance = serializers.DecimalField(max_digits=19, decimal_places=4, read_only=True)
    version = serializers.IntegerField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)

import socket
import ipaddress
from urllib.parse import urlparse

def validate_webhook_url(value):
    """
    Registration-time SSRF validation (C-6 fix).
    Delegates to _assert_safe_ip so the block-list is defined exactly once.
    """
    from django.conf import settings
    from ledger.services.webhooks import WebhookDispatchError, _assert_safe_ip

    parsed = urlparse(value)

    allow_http = getattr(settings, 'WEBHOOK_ALLOW_HTTP', False)
    if parsed.scheme == 'http' and not allow_http:
        raise serializers.ValidationError("URL must use https scheme.")
    if parsed.scheme not in ('http', 'https'):
        raise serializers.ValidationError(f"Unsupported URL scheme.")

    hostname = parsed.hostname
    if not hostname:
        raise serializers.ValidationError("Invalid URL hostname.")

    try:
        resolved_ip = socket.gethostbyname(hostname)
    except socket.gaierror:
        raise serializers.ValidationError("Could not resolve hostname.")

    try:
        _assert_safe_ip(resolved_ip)
    except WebhookDispatchError as exc:
        raise serializers.ValidationError(str(exc))

    return value



class WebhookEndpointSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    url = serializers.URLField(validators=[validate_webhook_url])
    event_types = serializers.ListField(
        child=serializers.ChoiceField(choices=['entry.posted', 'entry.reversed']),
        allow_empty=False
    )
    is_active = serializers.BooleanField(default=True)
    created_at = serializers.DateTimeField(read_only=True)


class WebhookEndpointCreateSerializer(WebhookEndpointSerializer):
    secret = serializers.CharField(read_only=True)


class AccountBalanceSerializer(serializers.Serializer):
    balance = StrictStringDecimalField(max_digits=28, decimal_places=4, read_only=True)
    debit_total = StrictStringDecimalField(max_digits=28, decimal_places=4, read_only=True)
    credit_total = StrictStringDecimalField(max_digits=28, decimal_places=4, read_only=True)
    snapshot_used = serializers.BooleanField(read_only=True)
