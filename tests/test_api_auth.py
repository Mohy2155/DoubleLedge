import datetime
import uuid

import jwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.urls import path
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.response import Response
from rest_framework.test import APIClient

from api.authentication import TenantJWTAuthentication
from api.decorators import tenant_transaction

# Generate a temporary RSA key pair for testing
private_key = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
    backend=default_backend()
)
public_key = private_key.public_key()

private_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)
public_pem = public_key.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo
)

@api_view(['GET'])
@authentication_classes([TenantJWTAuthentication])
@tenant_transaction()
def dummy_view(request):
    return Response({"tenant_id": str(request.user.id)})

urlpatterns = [
    path('test-auth/', dummy_view),
]

@pytest.fixture(autouse=True)
def override_jwt_settings(settings):
    settings.JWT_PUBLIC_KEY = public_pem
    settings.ROOT_URLCONF = __name__

@pytest.fixture
def api_client():
    return APIClient()

def generate_token(tenant_id, expired=False, use_wrong_key=False):
    payload = {
        "tenant_id": str(tenant_id),
        "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=-10 if expired else 3600)
    }
    
    key_to_use = private_pem
    if use_wrong_key:
        wrong_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend()
        )
        key_to_use = wrong_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        
    return jwt.encode(payload, key_to_use, algorithm="RS256")

@pytest.mark.django_db
def test_missing_token_fails(api_client):
    response = api_client.get('/test-auth/')
    assert response.status_code == 401

@pytest.mark.django_db
def test_expired_token_fails(api_client):
    tenant_id = uuid.uuid4()
    token = generate_token(tenant_id, expired=True)
    response = api_client.get('/test-auth/', HTTP_AUTHORIZATION=f'Bearer {token}')
    assert response.status_code == 401
    assert "expired" in str(response.data)

@pytest.mark.django_db
def test_invalid_signature_fails(api_client):
    tenant_id = uuid.uuid4()
    token = generate_token(tenant_id, use_wrong_key=True)
    response = api_client.get('/test-auth/', HTTP_AUTHORIZATION=f'Bearer {token}')
    assert response.status_code == 401

@pytest.mark.django_db
def test_valid_token_succeeds(api_client):
    tenant_id = uuid.uuid4()
    token = generate_token(tenant_id)
    response = api_client.get('/test-auth/', HTTP_AUTHORIZATION=f'Bearer {token}')
    assert response.status_code == 200
    assert response.data['tenant_id'] == str(tenant_id)

@pytest.mark.django_db
def test_malformed_token_header_fails(api_client):
    tenant_id = uuid.uuid4()
    token = generate_token(tenant_id)
    response = api_client.get('/test-auth/', HTTP_AUTHORIZATION=f'Token {token}')
    assert response.status_code == 401

    response = api_client.get('/test-auth/', HTTP_AUTHORIZATION=f'Bearer {token} extra')
    assert response.status_code == 401
