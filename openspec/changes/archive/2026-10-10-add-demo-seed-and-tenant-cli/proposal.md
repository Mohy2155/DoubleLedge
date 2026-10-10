# Proposal

## Why
The app has an API and Swagger docs but no way to get a tenant, a token or
data without hand-written SQL. Tenant creation under RLS has no production
path today. A reviewer cannot run a demo end to end.

## What Changes
- `create_tenant` management command: creates a tenant (client-generated
  UUID, `app.tenant_id` set before INSERT) and prints a dev JWT.
- `seed_demo` management command: creates a small chart of accounts,
  posts a few journal entries and authorizes one hold, idempotently.
- Dev-only JWT signing helper and gitignored dev keypair under `scratch/`.
- README quick-start for the demo.

## Out of scope
- Security or hardening changes (tracked in README "Known limitations").
- Account/tenant creation endpoints, UI, trial balance, webhook UI.

## Capabilities
`skip_specs`: developer tooling, no capability delta.

## Impact
- Local demo only. No schema change, no migration, no runtime-role change.
