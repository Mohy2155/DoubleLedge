from rest_framework import serializers


class StrictStringDecimalField(serializers.DecimalField):
    """
    A Decimal field that ONLY accepts JSON strings, never floats or ints,
    preventing any precision loss before deserialization.
    """
    def to_internal_value(self, data):
        if not isinstance(data, str):
            raise serializers.ValidationError("Monetary amounts must be provided strictly as strings.")
        return super().to_internal_value(data)

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
