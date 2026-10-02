# Spec Delta

## Purpose

Reliably delivers asynchronous events to external webhook endpoints using an at-least-once delivery guarantee, polling from a transactional outbox queue.

## ADDED Requirements

### Requirement: At-least-once event delivery
The system MUST dispatch pending events to matching webhook endpoints and retry failures using exponential backoff.

#### Scenario: Successful delivery
- **WHEN** an outbox event is successfully dispatched and receives a 2xx HTTP status code
- **THEN** the system marks the event as DELIVERED and stops retrying

#### Scenario: Retry on failure
- **WHEN** a webhook dispatch fails (timeout or non-2xx status code)
- **THEN** the system increments the retry count and reschedules the event with exponential backoff

### Requirement: Payload signature verification
The system MUST sign every webhook payload with the endpoint's HMAC-SHA256 secret.

#### Scenario: Dispatching signed payload
- **WHEN** the system makes a POST request to a webhook endpoint
- **THEN** it includes an `X-Signature-SHA256` header containing the valid HMAC signature of the canonical JSON payload

### Requirement: Concurrent dispatch safety
The system MUST prevent duplicate concurrent processing of the same outbox event by multiple workers.

#### Scenario: Concurrent worker claim
- **WHEN** multiple Celery workers poll the outbox simultaneously
- **THEN** they claim non-overlapping batches of events using `SELECT ... FOR UPDATE SKIP LOCKED`
