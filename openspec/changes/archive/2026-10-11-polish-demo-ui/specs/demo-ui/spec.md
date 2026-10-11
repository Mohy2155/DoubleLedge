# Demo UI Specification

## ADDED Requirements

### Requirement: Orientation
The demo page SHALL present the product name, a one-line description, a
link to the API documentation, and a guide describing the four showcase
flows.

#### Scenario: First visit
- **WHEN** a user opens `/demo/` before loading any data
- **THEN** the header, the API docs link and the expanded "Try it" guide
  are visible

### Requirement: Ledger precision preserved
The demo page SHALL display monetary amounts exactly as returned by the
API (four decimal places) and SHALL send amounts to the API as strings.

#### Scenario: Amount display
- **WHEN** an account has a posted balance of `890.0000`
- **THEN** the table shows `890.0000`, right-aligned with tabular figures

### Requirement: Safe transfer defaults
The transfer form SHALL NOT preselect an account in either dropdown, and
SHALL disable submission while an account is unset, both accounts are
identical, or the amount is empty.

#### Scenario: Same account selected
- **WHEN** the same account is chosen as source and destination
- **THEN** the Post Transfer button is disabled

### Requirement: Action feedback
The demo page SHALL show a success notification after each successful
action, SHALL show API error messages verbatim, and SHALL disable an
action's button while its request is in flight.

#### Scenario: Successful transfer
- **WHEN** a transfer returns HTTP 201
- **THEN** a success toast is shown and the accounts table is refreshed

#### Scenario: Rejected request
- **WHEN** the API rejects a request
- **THEN** the API's message is shown unmodified and the button is
  re-enabled

### Requirement: Readable holds
The holds table SHALL show the account name (falling back to the id),
the amount and captured amount, the expiry time, a status badge, and
actions only for pending holds.

#### Scenario: Finished hold
- **WHEN** a hold is CAPTURED, VOIDED or EXPIRED
- **THEN** it shows a status badge and no Capture or Void buttons

### Requirement: Request behavior unchanged
The presentation change SHALL NOT alter any request URL, method, header,
body, idempotency-key generation or response-handling logic.

#### Scenario: Logic freeze
- **WHEN** the diff for this change is inspected for `fetch(`, headers
  and request bodies
- **THEN** no non-whitespace line differs from `main`

### Requirement: Responsive layout
The demo page SHALL render in one column at widths of 900px or less and
SHALL NOT scroll horizontally at the page level.

#### Scenario: Narrow viewport
- **WHEN** the viewport is 390px wide
- **THEN** panels stack vertically and wide tables scroll inside their
  own panel.

### Requirement: Safe hold authorization defaults
The Authorize Hold form SHALL NOT preselect an account in its dropdown, showing
a "Select account" placeholder instead, and SHALL disable the Authorize Hold button
while no account is selected.

#### Scenario: No account selected for hold
- **WHEN** no account has been selected in the Authorize Hold dropdown
- **THEN** the Authorize Hold button is disabled

### Requirement: Holds table account name resolution
The holds table SHALL show the account name resolved from accounts data already
loaded in memory, falling back to the 8-character ID if not found, and SHALL
re-render when accounts data arrives if holds rendered first.

#### Scenario: Account name resolved in holds table
- **WHEN** holds and accounts data are loaded
- **THEN** the holds table displays the corresponding account name instead of the raw account ID

