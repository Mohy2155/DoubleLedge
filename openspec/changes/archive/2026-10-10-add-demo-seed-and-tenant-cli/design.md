# Design

## create_tenant
- Runs as the owner (`DB_USER=postgres`), not the runtime role, because
  inserting into `tenants` under RLS needs the owner or a context set
  before the INSERT.
- Generate the UUID in Python, `set_config('app.tenant_id', id, true)` in
  the same atomic block, then INSERT.
- Dev JWT: RS256, claims `tenant_id` (UUID string) and `exp` (both
  required by `TenantJWTAuthentication`). Signed with a private key
  matching `JWT_PUBLIC_KEY` (`/app/scratch/pub.pem` in compose). If a
  matching private key already exists locally, use it; otherwise
  generate a new keypair under `scratch/` and replace `pub.pem`.
  `scratch/` stays gitignored. Default token lifetime: 24h, configurable.

## seed_demo
- Input: tenant id. Runs inside `atomic()` with tenant context set.
- Accounts: Cash (ASSET), Customer Deposits (LIABILITY), Revenue
  (REVENUE), Expenses (EXPENSE).
- Entries via `post_entry` with deterministic reference ids so reruns hit
  the idempotency path and do nothing.
- One hold via `authorize_hold`.

## Risks
- Command run as the wrong DB role: document the exact compose command.
- Dev private key committed by accident: gitignore `scratch/`.
