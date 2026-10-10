# Proposal

## Why
The app has an API and Swagger docs but nothing a reviewer can click.
A minimal UI makes the ledger visible and demoable.

## What Changes
- Read-only `GET /api/v1/accounts/` and `GET /api/v1/holds/` list actions.
- A single-page demo UI at `/demo/` (plain HTML + JS, no build step).
- `create_tenant --tenant_id` mints a fresh token for an existing tenant.

## Out of scope
- Account/tenant creation UI, webhook UI, trial balance, auth flows.
- Hardening work (tracked in README "Known limitations").

## Capabilities
`skip_specs`: demo tooling plus two read endpoints.

## Impact
No schema or migration changes. No runtime-role changes.
