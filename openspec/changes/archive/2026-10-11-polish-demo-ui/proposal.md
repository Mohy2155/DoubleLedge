# Proposal: Polish the Demo UI

## Why
The demo UI at `/demo/` works end to end, but it reads as a test harness
rather than a product. There is no title or orientation, numeric columns
are not aligned, dropdowns default to a meaningless Cash-to-Cash transfer,
holds are shown as bare UUIDs, and successful actions give no feedback.
A reviewer opening the project for the first time should understand what
the ledger does within about 30 seconds, and the screenshots in the README
should show a finished-looking product.

## What Changes
- **Orientation:** a header (name, tagline, link to `/api/docs/`) and a
  collapsible "Try it" guide that walks through the four showcase flows.
- **Readability:** right-aligned, tabular-figure numerics; shortened ids
  with copy buttons; account names instead of ids; consistent status badges.
- **Guidance:** safer form defaults, a short debit/credit helper, and a
  client-side guard against same-account transfers.
- **Feedback:** success toasts, in-flight button states, and empty states.
- **Layout:** a max-width container, a responsive stack under 900px,
  visible focus rings, and a collapsed "Connected" token bar.

## Out of Scope
- Any change to request URLs, methods, headers, bodies, idempotency-key
  generation, or response handling.
- Any backend, schema, migration, or API change.
- New dependencies, CDN assets, web fonts, or a build step.
- Dark mode, animations, charts, or a JS framework.
- Security or hardening work (tracked under README "Known limitations").

## Capabilities
Adds a small `demo-ui` capability spec (presentation requirements only).

## Impact
- **Files:** `api/templates/demo/index.html` only.
- **Risk:** low. The only way to break behavior is to touch the JS that
  builds requests, which the tasks forbid and verify with a diff check.
- **Verification:** existing suite stays green; manual click-through of
  the four flows; screenshots captured for the README.
