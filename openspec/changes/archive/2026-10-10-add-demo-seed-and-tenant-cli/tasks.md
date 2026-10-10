# Tasks

## 1. Discovery (read-only)
- [x] 1.1 List registered routes, ViewSet actions, and the JWT claims
      `api/authentication.py` expects.

## 2. Commands
- [x] 2.1 `create_tenant` with dev JWT output and gitignored keypair.
      Must print the token and the tenant id.
- [x] 2.2 `seed_demo`, idempotent. Must print every created account id
      and the hold id, because the API has no list endpoints.

## 3. Verification
- [x] 3.1 Blank-volume boot, run both commands, curl accounts, post an
      entry and list holds with the printed token.
- [x] 3.2 Full non-redteam suite green with `--create-db`.

## 4. Docs
- [x] 4.1 README quick-start section.
