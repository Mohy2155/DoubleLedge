# Design: Polish the Demo UI

## 1. Principles
1. **Presentation only.** The request/response layer is frozen. The change
   wraps, labels and styles what already exists.
2. **Honest data.** Show only what the API returns. Where a column depends
   on a field that may be absent, the column is omitted rather than faked.
3. **Ledger precision is preserved.** Amounts stay at four decimals and are
   never rounded or reformatted into floats.
4. **Single file, zero dependencies.** One HTML file, inline CSS and JS,
   system fonts, no external requests.

## 2. Layout

```

 DoubleLedge                           API docs    Connected 
 Multi-tenant double-entry ledger  holds  sharding           

  Try it  (collapsible 4-step guide)                          

 Accounts                       Statement  <account name>    
  Name  Type  Posted          Line #  Direction  Amount  
  Held  Available  [Stmt]       Running balance            
                               
 Transfer                       Holds                         
  From / To / Amount / Desc      Authorize form               
  helper text  [Post]           Table: id  account  amount 
                                  expires  status  actions 

```

- Container: `max-width: 1200px; margin: 0 auto;` with 24px page padding.
- Two columns above 900px; single column below, in the order
  Accounts, Transfer, Statement, Holds.

## 3. Visual tokens

| Token | Value | Use |
|---|---|---|
| `--bg` | `#f6f8fa` | page background |
| `--surface` | `#ffffff` | panels |
| `--border` | `#d8dee4` | panel and table borders |
| `--text` | `#1f2328` | body text |
| `--muted` | `#59636e` | secondary text (4.5:1 on white) |
| `--accent` | `#1f6feb` | primary buttons, links, focus ring |
| `--ok` / `--ok-bg` | `#1a7f37` / `#dafbe1` | CAPTURED, success toast |
| `--warn` / `--warn-bg` | `#9a6700` / `#fff8c5` | PENDING |
| `--bad` / `--bad-bg` | `#cf222e` / `#ffebe9` | VOIDED, EXPIRED, errors |

Typography: `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`;
monospace stack (`ui-monospace, "SF Mono", Consolas, monospace`) for ids
and amounts. Base 14px; panel headings 18px/600; table headers 12px
uppercase with letter-spacing. Numerics use
`font-variant-numeric: tabular-nums` and `text-align: right`.

## 4. Components

**Header.** Title, one-line tagline, "API docs" link to `/api/docs/`, and a
connection pill (grey "Not connected" / green "Connected  tenant abcd1234").

**Try-it guide.** A `<details>` element, closed by default after the first
successful load, open before it. Four numbered steps:
1. Post a transfer between two accounts and watch both balances change.
2. Authorize a hold: Available drops, Posted does not.
3. Capture part of a hold: the remainder is released immediately.
4. Void another hold: its amount returns to Available.

**Token bar.** Before connecting: a password input and **Load Data**. After
a successful load it collapses to the connection pill plus a **Change**
button that restores the input. The token stays in memory only.

**Accounts table.** Columns: Name (with 8-char id, copy button), Type,
Posted, Held, Available, Action. The Held column is rendered only if the
list response includes `pending_holds`; otherwise it is omitted. The
selected row is highlighted (`aria-selected`) and drives the Statement.

**Transfer form.** Both selects start on a disabled "Select account"
placeholder. A muted helper line reads: "Credits the first account and
debits the second; the effect on each balance depends on its account type."
The submit button is disabled while either account is unset, both are the
same, or the amount is empty. This is a convenience guard only; the server
remains the source of truth for validation.

**Holds table.** Columns: Hold (8-char id + copy, description beneath),
Account (name resolved client-side from the accounts already loaded, id
as fallback), Amount (with captured amount beneath), Expires, Status,
Actions. Badges: PENDING amber, CAPTURED green, VOIDED red, EXPIRED
grey-red. Finished holds show a muted "Closed". Capture and Void buttons
appear only on PENDING holds.

**Statement.** Title is the account name with the id as muted text. First
column is "Line #". Direction renders as a small badge. A date column is
included only if the response carries a timestamp. When the response
includes enough to show it, the closing balance appears in a footer row.

**Toasts.** A fixed top-right stack, 4 seconds, `role="status"` for
success and `role="alert"` for errors. Error text is the API message
verbatim (no rewriting), with the existing banner behavior unchanged.

**Buttons.** Primary (accent), secondary (grey), danger (red outline) for
Void. In-flight requests set `disabled` and `aria-busy="true"` and restore
afterward, including on error.

## 5. Data handling rules (frozen)
- Every `fetch()` URL, method, header and body is byte-for-byte unchanged.
- `Idempotency-Key` generation is unchanged.
- Amounts remain strings from input to request body.
- Rendering code may be restructured; request-building code may not.
- Account-name resolution for holds is a client-side lookup over data
  already fetched. No new endpoint is called.

## 6. Accessibility
- Visible `:focus-visible` outline (2px accent, 2px offset).
- Labels bound to inputs; buttons are real `<button>` elements.
- Status never relies on color alone (badge text is always present).
- Minimum 4.5:1 contrast for body and muted text.

## 7. Responsive behavior
- At or below 900px the grid collapses to one column.
- Wide tables scroll horizontally inside their panel
  (`overflow-x: auto`); the page itself never scrolls sideways.

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Accidentally altering request code | Task 3.1 diffs every `fetch()`, header and body line; must be empty |
| Held or date column data absent | Columns rendered conditionally, omitted otherwise |
| Same-account guard hides a server error path | Guard is UI-only; server validation untouched |
| Layout regressions on small screens | Manual check at 1280, 900 and 390px widths |

## 9. Alternatives considered
- **A CSS framework or CDN:** rejected (extra dependency, offline demo).
- **Splitting into CSS/JS files:** rejected; one file keeps the demo
  trivially portable and reviewable.
- **Dark mode:** rejected for scope; tokens are variables, so it can be
  added later.
