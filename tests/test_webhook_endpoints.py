import pytest
from rest_framework.test import APIClient
from ledger.models import Tenant, WebhookEndpoint

from api.authentication import TenantUser

@pytest.fixture
def client():
    return APIClient()

@pytest.fixture
def tenant():
    return Tenant.objects.create(name="Test Tenant")

@pytest.fixture
def auth_client(client, tenant):
    client.force_authenticate(user=TenantUser(tenant.id))
    return client

@pytest.mark.django_db
def test_create_webhook_endpoint(auth_client, monkeypatch):
    # Mock SSRF DNS resolution to bypass real network
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")
    
    response = auth_client.post('/api/v1/webhook-endpoints/', {
        'url': 'https://example.com/webhook',
        'event_types': ['entry.posted']
    }, format='json')
    
    assert response.status_code == 201
    assert response.data['url'] == 'https://example.com/webhook'
    assert response.data['event_types'] == ['entry.posted']
    assert response.data['is_active'] is True
    assert 'secret' in response.data
    assert response.data['secret'].startswith('whsec_')
    
    # Secret must NOT be returned on list
    response_list = auth_client.get('/api/v1/webhook-endpoints/')
    assert response_list.status_code == 200
    assert len(response_list.data) == 1
    assert 'secret' not in response_list.data[0]
    
    # Secret must NOT be returned on retrieve
    ep_id = response.data['id']
    response_get = auth_client.get(f'/api/v1/webhook-endpoints/{ep_id}/')
    assert response_get.status_code == 200
    assert 'secret' not in response_get.data

@pytest.mark.django_db
def test_duplicate_webhook_returns_409(auth_client, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")
    
    url = 'https://example.com/webhook'
    
    res1 = auth_client.post('/api/v1/webhook-endpoints/', {'url': url, 'event_types': ['entry.posted']}, format='json')
    assert res1.status_code == 201
    
    res2 = auth_client.post('/api/v1/webhook-endpoints/', {'url': url, 'event_types': ['entry.posted']}, format='json')
    assert res2.status_code == 409
    assert res2.data['error']['code'] == 'conflict'

@pytest.mark.django_db
def test_webhook_url_ssrf_validation(auth_client, monkeypatch):
    import socket

    # Private RFC-1918 IP — always blocked regardless of scheme
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "192.168.1.1")
    res = auth_client.post('/api/v1/webhook-endpoints/', {
        'url': 'https://internal.local', 'event_types': ['entry.posted']
    }, format='json')
    assert res.status_code == 400
    assert 'url' in res.data['error']['message']

    # Loopback — always blocked
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "127.0.0.1")
    res = auth_client.post('/api/v1/webhook-endpoints/', {
        'url': 'https://localhost', 'event_types': ['entry.posted']
    }, format='json')
    assert res.status_code == 400
    assert 'url' in res.data['error']['message']

    # Cloud metadata endpoint — link-local, always blocked
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "169.254.169.254")
    res = auth_client.post('/api/v1/webhook-endpoints/', {
        'url': 'https://metadata.google.internal', 'event_types': ['entry.posted']
    }, format='json')
    assert res.status_code == 400
    assert 'url' in res.data['error']['message']


@pytest.mark.django_db
def test_webhook_invalid_event_type(auth_client, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")
    
    res = auth_client.post('/api/v1/webhook-endpoints/', {
        'url': 'https://example.com',
        'event_types': ['invalid.event']
    }, format='json')
    assert res.status_code == 400
    assert 'event_types' in res.data['error']['message']

@pytest.mark.django_db
def test_webhook_idempotency_key_exemption(auth_client, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")
    # Notice we don't provide HTTP_IDEMPOTENCY_KEY header
    res = auth_client.post('/api/v1/webhook-endpoints/', {
        'url': 'https://example.com/webhook2',
        'event_types': ['entry.posted']
    }, format='json')
    assert res.status_code == 201

@pytest.mark.django_db
def test_delete_endpoint(auth_client, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")
    
    res = auth_client.post('/api/v1/webhook-endpoints/', {
        'url': 'https://example.com',
        'event_types': ['entry.posted']
    }, format='json')
    assert res.status_code == 201
    ep_id = res.data['id']
    
    res_del = auth_client.delete(f'/api/v1/webhook-endpoints/{ep_id}/')
    assert res_del.status_code == 204
    
    res_get = auth_client.get(f'/api/v1/webhook-endpoints/{ep_id}/')
    assert res_get.status_code == 404

from ledger.models import OutboxEvent, WebhookDelivery

@pytest.mark.django_db
def test_cross_tenant_access_returns_404(auth_client, tenant, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")
    
    other_tenant = Tenant.objects.create(name="Other Tenant")
    ep = WebhookEndpoint.objects.create(
        tenant=other_tenant,
        url="https://example.com/other",
        secret="whsec_123",
        event_types=["entry.posted"]
    )
    
    # Try to GET, UPDATE, DELETE as 'tenant'
    res_get = auth_client.get(f'/api/v1/webhook-endpoints/{ep.id}/')
    assert res_get.status_code == 404
    
    res_upd = auth_client.put(f'/api/v1/webhook-endpoints/{ep.id}/', {'url': 'https://example.com/x', 'event_types': ['entry.posted']}, format='json')
    assert res_upd.status_code == 404
    
    res_del = auth_client.delete(f'/api/v1/webhook-endpoints/{ep.id}/')
    assert res_del.status_code == 404

@pytest.mark.django_db
def test_delete_endpoint_cascades_deliveries(auth_client, tenant, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")
    
    res = auth_client.post('/api/v1/webhook-endpoints/', {
        'url': 'https://example.com',
        'event_types': ['entry.posted']
    }, format='json')
    ep_id = res.data['id']
    
    # Create an OutboxEvent and a WebhookDelivery for this endpoint
    event = OutboxEvent.objects.create(tenant=tenant, type='entry.posted', payload={})
    WebhookDelivery.objects.create(
        tenant=tenant,
        event=event,
        endpoint_id=ep_id,
        status='PENDING'
    )
    
    assert WebhookDelivery.objects.filter(endpoint_id=ep_id).count() == 1
    
    auth_client.delete(f'/api/v1/webhook-endpoints/{ep_id}/')
    
    assert WebhookDelivery.objects.filter(endpoint_id=ep_id).count() == 0
