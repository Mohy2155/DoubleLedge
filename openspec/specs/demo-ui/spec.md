# demo-ui Specification

## Purpose
TBD - created by archiving change polish-demo-ui. Update Purpose after archive.

## Requirements

### Requirement: Orientation
The demo page SHALL present the product name, a one-line description, a
link to the API documentation, and a guide describing the showcase flows,
including currency conversion.

#### Scenario: First visit
- **WHEN** a user opens `/demo/` before loading any data
- **THEN** the header, the API docs link and the expanded "Try it" guide
  are visible

#### Scenario: Conversion step in the guide
- **WHEN** a user reads the "Try it" guide
- **THEN** it includes a step for converting currency between Cash and
  Cash EUR

### Requirement: Ledger precision preserved
The demo page SHALL display monetary amounts exactly as returned by the
API (four decimal places) and SHALL send amounts to the API as strings.

#### Scenario: Amount display
- **WHEN** an account has a posted balance of `890.0000`
- **THEN** the table shows `890.0000`, right-aligned with tabular figures

### Requirement: Safe transfer defaults
The transfer form SHALL NOT preselect an account in either dropdown, and
SHALL disable submission while an account is unset, both accounts are
identical, the two accounts have different currencies, or the amount is
empty.

#### Scenario: Same account selected
- **WHEN** the same account is chosen as source and destination
- **THEN** the Post Transfer button is disabled

#### Scenario: Accounts in different currencies
- **WHEN** a USD account and a EUR account are chosen as source and destination
- **THEN** the Post Transfer button is disabled and a hint points to the
  currency conversion form

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

### Requirement: Account currency display
The accounts table SHALL show each account's currency, and the transfer
and hold amount labels SHALL name the currency of the selected account
instead of a fixed `USD`.

#### Scenario: Mixed-currency accounts
- **WHEN** a tenant has USD and EUR accounts
- **THEN** each row of the accounts table shows that account's currency code

### Requirement: Hold currency follows the account
The Authorize Hold form SHALL send the selected account's currency with
the hold request.

#### Scenario: Hold on a EUR account
- **WHEN** a user authorizes a hold on `Cash EUR`
- **THEN** the request carries currency `EUR` and the hold is created

### Requirement: Currency conversion form
The demo page SHALL provide a conversion form with source account,
destination account, amount and rate, plus an optional rate source. It
SHALL disable submission until both accounts are set and in different
currencies and the amount and rate are filled. It SHALL send amounts and
rates as strings.

#### Scenario: Successful conversion
- **WHEN** a user converts `100` from Cash to Cash EUR at rate `0.92`
- **THEN** a success notification shows both amounts, both currencies and
  the rate as returned by the API, and the accounts table refreshes with
  the FX trading accounts included

#### Scenario: Same-currency pair
- **WHEN** both selected accounts are USD
- **THEN** the Convert button is disabled

### Requirement: Recent conversions table
The demo page SHALL list the tenant's recent conversions with both
amounts and currencies, the rate, the rate source and the rate's as-of
time, and SHALL refresh the list after each successful conversion.

#### Scenario: After a conversion
- **WHEN** a conversion succeeds
- **THEN** it appears at the top of the recent conversions table with its
  rate stamp

### Requirement: Second-currency demo data
The demo seed SHALL create an empty `Cash EUR` asset account in addition
to the existing USD accounts. It SHALL leave the USD accounts' seeded
balances and the seeded hold unchanged.

#### Scenario: Fresh demo tenant
- **WHEN** a reviewer runs the demo bootstrap
- **THEN** the tenant has Cash, Customer Deposits, Revenue, Expenses and
  Cash EUR, and the USD balances match the walkthrough in the README
