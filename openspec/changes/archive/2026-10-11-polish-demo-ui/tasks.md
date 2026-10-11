# Tasks

## 1. Foundation
- [x] 1.1 Define CSS variables (tokens), base typography, container
      max-width, panel and button styles, and the 900px breakpoint.
- [x] 1.2 Add `:focus-visible` outlines and tabular-nums numeric styling.

## 2. Components
- [x] 2.1 Header with title, tagline, API docs link, connection pill.
- [x] 2.2 Collapsible "Try it" guide with the four steps.
- [x] 2.3 Token bar collapse to "Connected  tenant <8>" with Change.
- [x] 2.4 Accounts: right-aligned numerics, short id + copy, selected-row
      highlight, Held column when `pending_holds` is present.
- [x] 2.5 Transfer form: placeholders, helper line, disabled-state guard.
- [x] 2.6 Toasts, in-flight button states, empty states for all tables.
- [x] 2.7 Holds: account name column, expires column, short id + copy,
      status badges (incl. amber PENDING), "Closed" for finished holds.
- [x] 2.8 Statement: account-name title, "Line #" column, direction
      badges, closing-balance footer and date column only if data exists.
- [x] 2.9 Authorize Hold form: "Select account" placeholder (empty value);
      button disabled while no account is selected.
- [x] 2.10 Holds table: Account column shows account name from in-memory
      accounts data (fallback to 8-char id); re-render when accounts arrive.

## 3. Verification
- [x] 3.1 Logic freeze check: paste the diff of every line touching
      `fetch(`, headers, request bodies and idempotency-key generation.
      Must be empty or whitespace-only.
- [x] 3.2 `git diff --stat main...HEAD` shows only the template and this
      change's docs.
- [x] 3.3 Full non-redteam suite green with `--create-db` (container).
- [x] 3.4 Manual click-through of all four flows: transfer, authorize,
      partial capture, void. Record any visual defects.
- [x] 3.5 Responsive check at 1280px, 900px and 390px widths.

## 4. Docs
- [x] 4.1 Capture four README screenshots: accounts overview, transfer
      success, hold authorized (Available dropped), partial capture.
- [x] 4.2 README: embed the screenshots in the demo section.
