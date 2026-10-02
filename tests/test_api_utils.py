import uuid

from django.db.models import ObjectDoesNotExist
from rest_framework.exceptions import APIException

from api.exceptions import custom_exception_handler
from api.serializers import TransactionLineSerializer
from api.utils import generate_idempotency_hash
from ledger.services.posting import PostingError


class MockException(APIException):
    status_code = 400
    default_code = 'invalid'

def test_custom_exception_handler():
    # Test PostingError mapping
    exc = PostingError("unbalanced_entry")
    response = custom_exception_handler(exc, None)
    assert response is not None
    assert response.status_code == 422
    assert response.data['error']['code'] == 'unbalanced_entry'

    # Test ObjectDoesNotExist mapping
    exc = ObjectDoesNotExist()
    response = custom_exception_handler(exc, None)
    assert response is not None
    assert response.status_code == 404
    assert response.data['error']['code'] == 'not_found'

    # Test DRF default mapping
    exc = MockException("Something went wrong")
    response = custom_exception_handler(exc, None)
    assert response is not None
    assert response.status_code == 400
    assert response.data['error']['code'] == 'invalid'

def test_strict_string_decimal_field():
    serializer = TransactionLineSerializer(data={
        "account_id": str(uuid.uuid4()),
        "amount": 100.50,  # Float!
        "direction": "DEBIT"
    })
    assert not serializer.is_valid()
    assert "Monetary amounts must be provided strictly as strings." in str(serializer.errors)

    serializer = TransactionLineSerializer(data={
        "account_id": str(uuid.uuid4()),
        "amount": "100.50",  # String!
        "direction": "DEBIT"
    })
    assert serializer.is_valid()

def test_canonical_hashing():
    body1 = b'{"b": 2, "a": 1}'
    body2 = b'{"a": 1, \n"b": 2}'
    
    hash1 = generate_idempotency_hash("POST", "/test", body1)
    hash2 = generate_idempotency_hash("POST", "/test", body2)
    assert hash1 == hash2

    # Different paths result in different hashes
    hash3 = generate_idempotency_hash("POST", "/test2", body1)
    assert hash1 != hash3
