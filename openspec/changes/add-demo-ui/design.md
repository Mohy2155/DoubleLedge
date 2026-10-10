# Design

## List endpoints
- Use the same `@tenant_transaction` decorator as the other actions, so
  RLS context is set.
- Accounts list: only top-level accounts (`parent_account_id IS NULL`),
  so shard rows are never returned. Balances come from
  `get_aggregate_balance()`, the same path as `retrieve`. Ordered by
  name, capped at 100.
- Holds list: newest first, capped at 100, optional `?status=`.

## UI
- One template served by a Django `TemplateView` at `/demo/`.
- Token is pasted into a box and kept in memory only (no storage).
- Views: accounts with balances; account statement; transfer form
  (journal entry); holds list with authorize, capture and void.
- The browser generates `Idempotency-Key` (`crypto.randomUUID()`) and
  sends amounts as strings. Errors from the API are shown verbatim.
- Same origin as the API, so no CORS changes.

## Risks
- A list endpoint could expose shard ids: covered by the
  `parent_account_id IS NULL` filter and a test.
