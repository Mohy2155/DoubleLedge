# Spec Delta

## Purpose

Allows tenants to register external HTTP endpoints to receive webhook event payloads, securely signed with a unique HMAC-SHA256 secret per endpoint.

## ADDED Requirements

### Requirement: Webhook endpoint management
The system MUST provide a REST API for tenants to create, read, update, and delete webhook destinations.

#### Scenario: Creating an endpoint
- **WHEN** a tenant submits a valid HTTPS URL and subscribed event topics
- **THEN** the system generates and stores a new webhook endpoint with a unique HMAC-SHA256 signing secret

#### Scenario: Enforcing HTTPS
- **WHEN** a tenant attempts to register an endpoint using plain HTTP
- **THEN** the system rejects the request to ensure payload confidentiality

### Requirement: Tenant isolation of endpoints
The system MUST restrict webhook endpoint access strictly to the tenant that owns them.

#### Scenario: Cross-tenant access attempt
- **WHEN** a tenant attempts to read or modify a webhook endpoint belonging to another tenant
- **THEN** the system returns a 404 Not Found due to Row-Level Security
