# ShipMatch — Quality Assurance Report

**Product:** ShipMatch (Django 5.2 web app that reads shipping documents, matches them to shipments, checks them against quotes and rules, routes them for approval and posts bills to QuickBooks or Xero)
**Report date:** 8 October 2026 · **Covers:** QA sessions 01–13 (3–8 October 2026) · **Status:** 13 sessions complete, 11 planned sessions remaining
**Source material:** one log per session (`qa/QA_SESSION_01.md` … `13.md`), the prioritised summary (`qa/QA_SUMMARY.md`), and the git history of the fixes.

---

## 1. At a glance

| | |
|---|---|
| Sessions completed | **13** (3–8 October 2026) |
| Remaining sessions planned | **11** (sessions 14–24, section 7) |
| Issues logged | **88** (numbers QA-001 … QA-089; QA-047 unused) |
| Fixed | **66** |
| Partly fixed or improved | **3** |
| Not a defect, or withdrawn after checking | **7** |
| Waiting for a product decision | **9** |
| Open | **3** |
| Severity mix | Critical 1 · High 16 · Medium 20 · Low/Medium 2 · Low or verify 49 |

**Headline results**

- The two most serious problems found in session 01 are fixed: one mistyped number could take the whole app down for a company (QA-001), and an organization admin could add another company's user and take over their account (QA-048).
- Every web page and action that was tested enforces roles and keeps companies apart. Cross-company requests returned "not found" in every test (sessions 01, 05, 09).
- Database behaviour was tested on the production engine for the first time in session 12: all migrations, the demo build and the automated test suite work on PostgreSQL 16, and two Postgres-only crashes were found and fixed.
- Remaining risk is concentrated in things that need outside accounts or devices (real mailboxes, Xero, a public webhook receiver, other browsers and screen readers, background workers). Those are the remaining sessions in section 6.

---

## 2. How the testing was done

| | |
|---|---|
| **Method** | Exploratory testing "like a real user" in the built-in browser (Chromium), plus HTTP clients and Django's test client for things a browser can't do well: file uploads, parallel requests, download contents, response headers, query counts. |
| **Rule for sessions 01–07** | **List only, fix nothing.** Each session added to one coverage map and used the next issue number, so the work could be repeated until the whole platform was covered. |
| **Fixing** | Started only when asked ("fix the critical issues first"), in stages; every fix batch has tests and was committed and pushed only when requested. Sessions 08–13 were fixed on the same day they were found, at the user's request. |
| **Environments** | Local server on SQLite with demo data (session 01–11, 13); a throwaway PostgreSQL 16 container (session 12); a second server with `DEBUG` off (sessions 08, 09); a 3,000-shipment synthetic data set (session 13). |
| **Test data** | Acme Imports (demo organization) for ordinary flows; an empty **Northwind Traders (test)** organization for anything destructive. Every session that changed data started from a database backup, and the data was restored afterwards when it was not meant to stay. |
| **Safety** | Email used the console backend (nothing left the machine). The only outside service touched was the QuickBooks **sandbox**, once, with the user's approval (session 02). No webhook was sent to an outside address. |
| **Automated tests** | The project's own suite (pytest) grew with each fix batch. Last full run completed: **1,069 passed, 1 skipped** on SQLite; **1,063 passed, 2 skipped** on PostgreSQL (one clock-dependent test, since made deterministic). The run for the session 13 changes is in progress. |

---

## 3. Session-by-session record

### Session 01 — Platform sweep (3 Oct)
- **Goal:** map the whole platform and find what is broken, without fixing.
- **Covered:** 25 areas in a coverage map: public pages, sign-in, dashboard, queue, shipment detail, documents, search and shortcuts, rates, savings, landed cost, disputes, month-end, audit log, team, settings, API, notifications, responsive checks, plus a smoke crawl of 44 pages and access-control checks (reviewer against admin; company against company).
- **Found:** QA-001 … QA-023.
- **Notable:** a 20-digit invoice total saved without complaint and then made the dashboard, Savings, Clients and shipment pages return HTTP 500 for everyone (QA-001, **Critical**); a traceback with a server file path was shown on an unreadable document (QA-002); the API reference page could not render (QA-004); false "container not on the bill of lading" errors on shared invoices (QA-003).
- **Data incident:** QA-001 corrupted two rows; they were repaired and the data restored after the user said so.

### Session 02 — Core approval loop (4 Oct)
- **Covered:** picking a shipment from the queue, correcting fields, accepting warnings, overriding errors, approve and reject, bulk approve, maker-checker, approval limits, reopening, posting bills to the **QuickBooks sandbox** (approved by the user), reading payment status back.
- **Found:** QA-024 … QA-031: a shipment shown as "Ready to approve" that could not be approved; one-click approve while the shortcut promised a confirmation; `SimpleLazyObject 3` in the audit "Record" column; mixed-currency totals run together; confirming a split silently accepting sibling shipments' warnings under your name; a half-posted shipment with no summary.

### Session 03 — Disputes and month-end (4 Oct)
- **Covered:** dispute drafts and lifecycle (first pass), month-end accruals, locking a period, vendor statements, statement upload, month-end settings, payments, adjustments.
- **Found:** QA-032 … QA-039: a period could be locked on its last day, and an empty or garbled `period` silently locked the latest month-end (locks are permanent); payment currency "DOLLARS" truncated to "DOL"; unreadable statement rows dropped without a warning; inconsistent error text; dispute "To" empty.
- **Data:** a lock was created by the garbled-period test; the session-03 backup was restored afterwards at the user's request.

### Session 04 — Intake and integrations (4 Oct)
- **Covered:** uploads (formats, sizes, ZIP archives, scans), the IMAP form, email forwarding addresses, the public API and API keys, webhook management, in the Northwind test organization.
- **Found:** QA-040 … QA-045: shipment numbers are one global sequence across companies; the IMAP form saved the mailbox (and password) even when the connection test failed; the API accepted nonsense filters; **uploads accepted any file that merely started with `%PDF`** (QA-043, the root of QA-002); environment-variable names shown to users.

### Session 05 — Accounts and admin (4 Oct)
- **Covered:** password rules and change, password reset, two-factor setup, recovery codes, lockout, team invitations and roles, organization settings, the demo `admin` account, all in the Northwind organization with every credential put back.
- **Found:** QA-046 … QA-052. **QA-048 (High, security):** an org admin could add any existing user to their company by email and then generate a password-set link or reset the 2FA of that shared account.

### Session 06 — Cross-cutting checks (4 Oct)
- **Covered:** HTML and accessibility audit of all 44 pages, rendered colour-contrast scan, keyboard-only use, responsive layouts at 375 / 768 / 1100 px, response headers, and a volume test with 600 generated shipments.
- **Found:** QA-053 … QA-064: unbranded CSRF error page; a blank 405 page after the session expired; `/my-work/` slow; muted text just under the accessibility contrast limit; skip link not moving focus; small tap targets; heading and title problems; `page=0` showing the last page.

### Session 07 — Closing the partial-coverage gaps (6 Oct)
- **Covered:** disputes (create, edit, reply, credit, resolve, close), rates (quotes, extras, charge names), landed-cost splits and methods, the month-end journal Excel file, statements as PDF and Excel, parallel uploads. The user asked for the partially tested features to be tested thoroughly; this was that pass.
- **Found:** QA-065 … QA-074: disputes accepting amounts above the invoice total; credits inflating "Recovered"; quotes accepting nonsense; **parallel identical uploads returning HTTP 500** (QA-074).
- **Result:** the platform's coverage map reached "all areas tested at least once" except those needing outside accounts.

### Fixing phase after session 07 (6–7 Oct)
Fixes were made in stages at the user's request: critical first (QA-001, 048), then QA-002/043/074, then the next batch (month-end locks, IMAP form, stale pages, typed amounts, API docs, approval wording), then everything else that was unambiguous (about 40 items), with a browser check of the layout fixes. See section 5 for commits.

### Session 08 — Webhooks, accounting settings, DEBUG off (7 Oct)
- **Covered:** webhook address validation (13 bad addresses all refused with a clear reason), webhook edit and remove, QuickBooks settings page, Xero page, security headers and 404 page with `DEBUG` off.
- **Found:** QA-075 (QuickBooks default expense account accepted any value and said "saved"), QA-076 (long host echoed in an error), QA-077 (API docs loaded Swagger from public CDNs). All fixed the same day.

### Session 09 — Static files, re-check, input and tenant sweeps (7 Oct)
- **Covered:** static files with `DEBUG` off (compressed, but cached only 60 s: QA-078), a browser re-check of the earlier fixes (all held), 120 search and filter requests with hostile text (no server errors, nothing reflected unescaped), cross-company requests (all "not found").
- **Found:** QA-078, fixed (hashed file names, immutable cache header).

### Session 10 — Concurrency (7 Oct)
- **Covered:** six simultaneous split confirmations, double approvals, approve against reject, parallel dispute creation, with parallel requests from a signed-in page.
- **Found:** QA-079 (SQLite "database is locked" → HTTP 500), QA-080 (a double approval recorded twice), QA-081 (approve and reject both recorded, shipment ended approved). Fixed with a row lock in a transaction and SQLite write-lock waiting; re-run in the browser, then the database was restored.

### Session 11 — Export safety, password reset, caching (8 Oct)
- **Covered:** spreadsheet formula injection across 4 CSV files, 3 Excel files and 3 API exports (all safe: formula text is neutralised), the password-reset flow (does not reveal which addresses have accounts), cache headers and the Back button after sign-out.
- **Found:** QA-082 (signed-in pages send no `Cache-Control`). Open.

### Session 12 — PostgreSQL (8 Oct)
- **Covered:** all migrations on an empty PostgreSQL 16 database, the full demo build, the whole automated suite (1,063 passed), the row-lock fixes on real row locks, and 411 forms submitted with long text, NUL characters, giant numbers and over-range integers, four crawls at once.
- **Found:** QA-083 (dispute settings crashed on a reply-to address over 254 characters), QA-084 (moving a document into a shipment another request was emptying gave a 500 and could delete a shipment just after a document landed). Fixed; the same crawls now give zero server errors.

### Session 13 — Behaviour at scale (8 Oct)
- **Covered:** 3,000 synthetic shipments, 6,000 documents, 30,000 fields and about 3,450 issues: timing and query counts for 22 pages and downloads.
- **Found:** QA-085 … QA-089 (speed). Paged lists were already fast (under half a second). Fixed: month-end report 16 s → 5.8 s and 6.3 MB → 143 KB; My work 9.4 s and 1,984 queries → 0.6–0.9 s and 193; API documents list 209 queries → 9; dashboard and Savings about 30% faster. QA-087 was withdrawn (an artefact of the synthetic data). Still slow: the Excel journal (about 8 s of openpyxl cell writing; the `lxml` library was tried and did not help) and the big exports.

---

## 4. Issue register

All 88 issue numbers. Severity is the latest understanding; "verify" means a product decision is needed. QA-047 was never used.

| ID | Severity | Issue | Session | Status | Fix |
|---|---|---|---|---|---|
| QA-001 | Critical | Editing an invoice field with a huge number takes the whole app down for everyone | 01 | Fixed | `0aea335` |
| QA-002 | High | Python traceback and server file path shown to users | 01 | Fixed | `0d04083` |
| QA-003 | High | False "Container not on the bill of lading" errors on the primary shipment of a shared invoice | 01 | Fixed | `fdbe801` |
| QA-004 | High | API reference page (/api/docs) cannot render | 01 | Fixed | `fdbe801` |
| QA-005 | Medium | Raw field keys and junk values in the activity/audit trail | 01 | Fixed | `fe46865` |
| QA-006 | Medium | Mobile (375 px): shipment page scrolls sideways | 01 | Fixed | `860e417` |
| QA-007 | Medium | "Show PDF" does nothing visible at ~1024 px and below | 01 | Fixed | `860e417` |
| QA-008 | Low | Dashboard "Waiting" column clipped at about 1024 px | 01 | Fixed | `fe46865` |
| QA-009 | Low | Month-end "Lock" button offered for a future period | 01 | Fixed | `fdbe801` |
| QA-010 | Low | Landed and Savings date ranges silently swapped or reset | 01 | Fixed | `fe46865` |
| QA-011 | Low | Landed per-unit costs shown with inconsistent decimals | 01 | Fixed | `fe46865` |
| QA-012 | Low | Time-zone list has 519 entries including deprecated aliases | 01 | Fixed | `fe46865` |
| QA-013 | Low | Developer wording (environment-variable names) in user messages | 01 | Fixed | `fe46865` |
| QA-014 | Low | @mention typed without picking a name does nothing, with no hint | 01 | Fixed | `fe46865` |
| QA-015 | Low | Duplicate-upload message names the wrong file | 01 | Fixed | `fe46865` |
| QA-016 | Low | /try/ and /signup/ return 404 (intended?) | 01 | Needs a decision |  |
| QA-017 | Low | Invalid Bearer key accepted when a browser session exists | 01 | Fixed | `fe46865` |
| QA-018 | Low | Unstyled native file-input control | 01 | Fixed | `fe46865` |
| QA-019 | Low | Same unreadable file listed twice | 01 | Not a defect: the two files had different bytes |  |
| QA-020 | Medium | Demo admin is a platform superuser | 01 | Needs a decision |  |
| QA-021 | Low | Dashboard shows "8 posted" and "17 posted bills not checked" | 01 | Explained: bills had never been polled |  |
| QA-022 | Medium | Payable total counts the whole shared invoice, not the shipment's share | 01 | Needs a decision |  |
| QA-023 | Low | Django DEBUG 404 page (development only) | 01 | Needs a decision |  |
| QA-024 | High | Shipment is labelled "Ready to approve" but cannot be approved | 02 | Fixed | `fdbe801` |
| QA-025 | High | Approve button approves in one click, no confirmation | 02 | Fixed | `fdbe801` |
| QA-026 | Low | Audit log "Record" column shows SimpleLazyObject 3 | 02 | Fixed | `fe46865` |
| QA-027 | Low | Denied actions are not written to the audit log | 02 | Needs a decision |  |
| QA-028 | Low | Mixed-currency totals run together | 02 | Fixed | `fe46865` |
| QA-029 | Medium | Confirming a split quietly accepts siblings' warnings under your name | 02 | Fixed | `fe46865` |
| QA-030 | Medium | Partly posted shipment shows no summary at the top | 02 | Fixed | `fe46865` |
| QA-031 | Low | QuickBooks redirect URI pinned to port 8000 (comes from .env) | 02 | Open |  |
| QA-032 | High | A period can be locked on its own last day, before it has ended | 03 | Fixed | `fdbe801` |
| QA-033 | High | A malformed or empty period silently locks the latest month-end | 03 | Fixed | `fdbe801` |
| QA-034 | Low | "Lock version 2" is offered when "Nothing has changed since it was locked" | 03 | Fixed | `fdbe801` |
| QA-035 | High | Payment currency is silently truncated | 03 | Fixed | `fdbe801` |
| QA-036 | Medium | Statement rows that can't be read are dropped without a warning | 03 | Fixed | `fe46865` |
| QA-037 | Low | No confirmation message after some actions | 03 | Not a defect: the messages exist and render |  |
| QA-038 | Low | Inconsistent error text on month-end settings | 03 | Fixed | `fe46865` |
| QA-039 | Low | Dispute draft "To" is empty | 03 | Fixed | `fe46865` |
| QA-040 | Medium | Shipment numbers are one global sequence across companies | 04 | Needs a decision |  |
| QA-041 | High | IMAP form saves the mailbox even when the connection test fails | 04 | Fixed | `fdbe801` |
| QA-042 | Low | API accepts nonsense filters | 04 | Fixed | `fe46865` |
| QA-043 | High | API (and web) upload accept files that only *start* like a PDF | 04 | Fixed | `0d04083` |
| QA-044 | Low | Env-var names in the scan message | 04 | Fixed | `fe46865` |
| QA-045 | Low | Nested ZIP message is confusing | 04 | Not a defect |  |
| QA-046 | Low | Password can be changed to the current password | 05 | Fixed | `fe46865` |
| QA-048 | High (security) | An org admin can add any existing user to their company by email, then reset that user's password | 05 | Fixed | `0aea335` |
| QA-049 | Low | Recovery codes are rendered as adjacent <span>s with no separator; entry is strict | 05 | Fixed | `fe46865` |
| QA-050 | Low | Anyone can lock anyone out | 05 | Needs a decision |  |
| QA-051 | Medium | No server-side length limit on the organization name | 05 | Fixed | `fe46865` |
| QA-052 | Low | A few settings forms give no visible result in a scripted submit | 05 | Not a defect: the messages exist and render |  |
| QA-053 | Low | Error messages use role="status" | 06 | Fixed | `fe46865` |
| QA-054 | High | CSRF failures show Django's default, unbranded page | 06 | Fixed | `fdbe801` |
| QA-055 | High | After the session expires mid-action, sign-in sends you to a blank page | 06 | Fixed | `fdbe801` |
| QA-056 | High | /my-work/ takes about 2 seconds | 06 | Fixed | `ebff5e8` |
| QA-057 | Medium | Shipment page eagerly loads pdf.js and the first PDF | 06 | Open (trade-off) |  |
| QA-058 | Low | Muted text is just under WCAG AA | 06 | Fixed | `fe46865` |
| QA-059 | Low | No dark mode or forced-colours support; font sizes are in px | 06 | Needs a decision |  |
| QA-060 | Low | Skip link doesn't move focus | 06 | Fixed | `fe46865` |
| QA-061 | Low | Small touch/click targets | 06 | Fixed | `fe46865` |
| QA-062 | Low | Heading structure and page titles | 06 | Fixed | `fe46865` |
| QA-063 | Low | Static files carry no cache headers or compression in this setup | 06 | Fixed | `e066d77` |
| QA-064 | Low | ?page=0 and negative pages show the last page | 06 | Fixed | `fe46865` |
| QA-065 | Medium | Disputed amount isn't checked against the invoice | 07 | Fixed | `fe46865` |
| QA-066 | Low | "Credit note" picker offers every document in the organization | 07 | Fixed | `fe46865` |
| QA-067 | Medium | A credit larger than the dispute is recorded and inflates the "Recovered" KPI | 07 | Fixed | `fe46865` |
| QA-068 | Medium | Rate quotes accept questionable data | 07 | Fixed | `fe46865` |
| QA-069 | Low | Editing an approved extra says "Nothing changed." although it saved | 07 | Fixed | `fe46865` |
| QA-070 | Low | A shipment's landed-cost method can't be set back to "organization setting" | 07 | Not a defect: the control exists |  |
| QA-071 | Low | Journal wording for goods lines | 07 | Fixed | `fe46865` |
| QA-072 | Low | Wrong reason shown for a ZIP bomb | 07 | Fixed | `fe46865` |
| QA-073 | Low | A text-only PDF is classed as a scan | 07 | Needs a decision |  |
| QA-074 | High | Parallel identical uploads cause HTTP 500 | 07 | Fixed | `0d04083` |
| QA-075 | Medium | QuickBooks "default expense account" accepts any value and says it was saved | 08 | Fixed | `9523af3` |
| QA-076 | Low | A long host name is echoed in full in the webhook error | 08 | Fixed | `9523af3` |
| QA-077 | Low (verify) | API reference page loads Swagger UI from jsdelivr and an icon from django-ninja.dev | 08 | Fixed | `9523af3` |
| QA-078 | Low | Static files are cached for only 60 seconds and have no content hash in the name | 09 | Fixed | `e066d77` |
| QA-079 | Medium | Concurrent writes on SQLite return HTTP 500 "database is locked" | 10 | Fixed | `3512428` |
| QA-080 | Medium | Approving the same shipment twice at once records it twice | 10 | Fixed | `3512428` |
| QA-081 | Medium | Approve and reject sent at the same moment are both recorded; the shipment ends approved | 10 | Fixed | `3512428` |
| QA-082 | Low (verify) | Signed-in pages and downloads carry no cache directive | 11 | Open (verify) |  |
| QA-083 | Medium | The dispute settings page crashes (HTTP 500) when the reply-to address is longer than 254 characters | 12 | Fixed | `92c0b91` |
| QA-084 | Low/Medium | Moving a document into a shipment that someone else is emptying at the same moment gives HTTP 500, and the emptied… | 12 | Fixed | `92c0b91` |
| QA-085 | High | The month-end accruals report takes 16 s and sends a 6.3 MB page; its CSV and Excel journal take 10–18 s | 13 | Fixed for the page and CSV; Excel journal still slow | `ebff5e8` |
| QA-086 | Medium | "My work" runs about 2,000 queries (9.4 s) for roughly 300 items | 13 | Fixed | `ebff5e8` |
| QA-087 | Low | The landed-cost report and its CSV/Excel run about 2,000 queries (8–11 s) | 13 | Withdrawn: caused by the synthetic test data |  |
| QA-088 | Low/Medium | API documents list and the three export endpoints are slow at scale | 13 | Fixed for the list; exports 3–4 s faster, still 7–10 s | `ebff5e8` |
| QA-089 | Low | The dashboard (1.8 s, 151 queries) and Savings (2.5 s) are noticeably slow at this size | 13 | Improved by about 30% | `ebff5e8` |

---

## 5. Where the fixes are

| Commit | Branch | What it contains |
|---|---|---|
| `0aea335` | earlier branch | QA-001, QA-048 |
| `0d04083` | earlier branch | QA-002, QA-043, QA-074 |
| `fdbe801` | earlier branch | month-end locks (032, 033, 009, 034), IMAP form (041), stale pages (054, 055), typed amounts (035), API docs (004), approval wording (003, 024, 025) |
| `fe46865`, `7a4122f`, `860e417` | `fix/qa-remaining` (merged) | the remaining unambiguous findings, including the layout fix for QA-006/007 and the summary |
| `9523af3`, `e066d77`, `bf4f141` | `fix/qa-session-08` | QA-075, 076, 077, 078 and the logs for sessions 08–09 |
| `3512428`, `3d0986c`, `92c0b91` | `fix/qa-session-08` | QA-079, 080, 081, 083, 084 and the logs for sessions 10–12 |
| `ebff5e8` | `fix/qa-session-13` | QA-085, 086, 088, 089 and the session 13 log |

---

## 6. Coverage map

✅ tested · 🟡 partly tested · ⬜ not tested

| Area | Sessions | Status | What is still open |
|---|---|---|---|
| Public pages, sign-in, password rules, lockout, 2FA, recovery codes | 01, 05, 11 | ✅ | |
| Password reset and invitations | 05, 11 | ✅ | |
| Access control by role, company isolation | 01, 05, 09 | ✅ | |
| Dashboard, review queue, shipment page, documents | 01, 02, 04, 09 | ✅ | |
| Approval loop (approve, reject, override, bulk, maker-checker, limits) | 02, 10, 12 | ✅ | |
| Posting to QuickBooks (sandbox) and reading payments | 02, 08 | 🟡 | disconnect and reconnect; double posting at once |
| Xero | 08 | ⬜ | needs Xero app keys |
| Rates, savings, landed cost, shared invoices | 01, 07, 13 | ✅ | |
| Disputes (full lifecycle, sending through the console backend) | 03, 07, 12 | ✅ | |
| Month-end, statements, payments, journal Excel | 03, 07, 13 | ✅ | Excel journal still slow |
| Uploads, formats, ZIP, scans, duplicates | 04, 07, 12 | ✅ | |
| Mailboxes: IMAP form, forwarding address | 04, 08 | 🟡 | real IMAP, Microsoft 365, Gmail, forwarding |
| Public API, keys, scopes, exports | 01, 04, 09, 11, 13 | ✅ | |
| Webhooks: setup and address validation | 01, 08 | ✅ | |
| Webhooks: real delivery, retries, replay | — | ⬜ | needs a public https receiver |
| Responsive (375 / 768 / 1100 px) | 01, 06, 08 | ✅ | Chromium only |
| Accessibility (structure, contrast, keyboard) | 06 | ✅ | other browsers, real screen reader |
| Security headers, `DEBUG` off, static files | 08, 09 | ✅ | |
| Cache headers after sign-out | 11 | 🟡 | other browsers (QA-082) |
| Concurrency (double submits, simultaneous decisions) | 07, 10, 12 | ✅ | posting twice |
| PostgreSQL | 12 | ✅ | performance on Postgres |
| Behaviour at scale (3,000 shipments) | 06, 13 | ✅ | much larger data; many users at once |
| Background workers (Redis, Celery, schedules) | — | ⬜ | |
| Billing, signup, trial, Stripe | — | ⬜ | |
| AI and OCR readers with real documents | — | ⬜ | costs provider credits |
| Backup, restore, upgrade path | — | ⬜ | |

---

## 7. Remaining QA sessions

Order is by value against effort. "Needs" lists what must be provided before the session can run.

| # | Session | What it will test | Needs | Effort |
|---|---|---|---|---|
| **14** | **Webhook delivery** | Real delivery of signed events, retries with growing pauses, the 20-failures switch-off and admin email, replay from the endpoint page, duplicate event ids, the signature check from the README example, slow or failing receivers. | A public https endpoint you control (a tunnel to a local receiver is enough). | Small |
| **15** | **Real mailboxes** | IMAP connect and sync, app passwords, Microsoft 365 sign-in, Gmail forwarding confirmation, forwarding address and inbound webhook providers, mail with odd attachments, duplicate mail, rate limiting. | Test mailbox accounts (Gmail/Outlook/IMAP) and app passwords. | Medium |
| **16** | **Accounting integrations** | QuickBooks disconnect, reconnect and token expiry; posting twice at once (idempotency); credit notes as vendor credits; payment sync; Xero connect, posting, payment read. | QuickBooks sandbox (exists); a Xero developer app and demo company. | Medium |
| **17** | **Other browsers and assistive technology** | Firefox, Safari, Edge, a phone; the pdf.js evidence viewer, uploads, keyboard shortcuts, print layouts; a screen reader pass (NVDA or VoiceOver); the Back button after sign-out (QA-082); dark mode / forced colours decision (QA-059). | The browsers and devices; a screen reader. | Medium |
| **18** | **Background workers** | The Docker stack's Redis and Celery workers: asynchronous document reading, retries, the hourly payment sync, scheduled digests and reminders, demo reset, behaviour when a worker dies mid-job. | Redis image download (about 40 MB) and the compose stack running. | Medium |
| **19** | **Production deployment rehearsal** | `docker-compose.prod.yml` with Caddy: TLS, HSTS and other headers, static caching through the proxy, `collectstatic` on deploy, required environment variables, health checks, backup script and a **restore drill**, an upgrade with migrations over existing data. | A throwaway domain or a local hostname; Docker. | Medium |
| **20** | **Load and performance on PostgreSQL** | The session 13 data set on PostgreSQL; many users at once (login, queue, approvals); report build times; memory; the remaining slow exports and the Excel journal (write-only mode or CSV). | Postgres container (image already downloaded); a load-test tool. | Medium |
| **21** | **Security deep-dive** | Role-by-endpoint matrix for every POST route, server-side request checks for every URL the server fetches, file-upload abuse (archive bombs, polyglot files), session handling (fixation, expiry, concurrent sessions), rate limits, dependency vulnerability scan, secrets in logs. | A dependency scanner (a download). | Large |
| **22** | **Billing and sign-up** | Trial and sign-up flow (QA-016: `/try/` and `/signup/` return 404; confirm intended), plans and limits, Stripe test mode, webhooks from Stripe, payment-failed banners, cancellation. | Stripe test keys. | Medium |
| **23** | **AI and OCR reading accuracy** | Extraction with the AI and OCR providers on real and scanned documents: accuracy report, vendor learning, cost, failure messages, text-light PDFs (QA-073). | Provider keys and credit; real sample documents. | Medium |
| **24** | **Final regression pass** | Re-run the full coverage map once every open item is decided and fixed; confirm the automated suite on SQLite and PostgreSQL; update this report. | — | Small |

Sessions 14, 16 (Xero part), 22 and 23 depend on accounts or keys you would have to supply; 17 depends on hardware. Sessions 18–21 and 24 can be run with what is already installed, apart from the downloads noted.

---

## 8. Open items and decisions

**Open defects**

- **QA-031** QuickBooks redirect URI pinned to port 8000 (comes from .env). the redirect URI comes from the .env file; set it per environment.
- **QA-057** Shipment page eagerly loads pdf.js and the first PDF. pdf.js loads on every shipment page because the evidence highlighting depends on it.
- **QA-082** Signed-in pages and downloads carry no cache directive. add Cache-Control: no-store on signed-in pages and downloads, and re-check in other browsers.
- **QA-085 (rest)** The Excel month-end journal still takes 16–21 s: `openpyxl` needs about 8 s to save 127,000 cells and installing `lxml` did not help. Needs write-only mode or a CSV for the big sheet (session 20).
- **QA-088 (rest)** The shipments and documents exports still take 7–10 s for 3,000 shipments, because about 50,000 Django objects are built; a rewrite on plain queries would fix it (session 20).

**Decisions needed from the product owner**

- **QA-016** Are /try/ and /signup/ meant to be switched off? (the login page links to neither)
- **QA-020** Should the seeded demo admin be a platform superuser, or only when seeded with --superuser?
- **QA-022** Should approval limits use the whole shared invoice or only the shipment's share?
- **QA-023** Make sure every deployment runs with DEBUG off (the debug 404 page lists URLs).
- **QA-027** Should denied actions (403) be written to the audit log?
- **QA-040** Use a per-company shipment counter so numbers do not reveal other companies' volume?
- **QA-050** A known user can be locked out for 15 minutes by anyone; add per-address limits and shared (Redis) counters?
- **QA-059** Support dark mode and forced colours, and relative font sizes?
- **QA-073** Should a text-only PDF with little text be treated as a scan?

**Recommended before release:** decide QA-020 (demo `admin` is a platform superuser when seeded), QA-022 (which total approval limits use for a shared invoice), QA-040 (global shipment numbers reveal other companies' volume), QA-050 (anyone can lock out a known user; counters are in memory per process), and make sure deployments run with `DEBUG` off (QA-023).

---

## 9. Appendix

**A. Test accounts (local demo data only)**
`admin` (organization admin of Acme Imports, also a platform superuser when seeded), `approver` (limit USD 50,000), `reviewer`, and the owner of the empty Northwind Traders (test) organization (see `local-test-accounts.txt`). Passwords are in the README for local evaluation and must be changed on any server other people can reach.

**B. Re-running the work**
- Each session log starts with its method and the backup it used; restore with a plain file copy of `db.sqlite3` while the server is stopped.
- Scale data: a script that bulk-creates 3,000 shipments (kept out of the repository; the approach is in `qa/QA_SESSION_13.md`).
- PostgreSQL: `docker run -d --name shipmatch-qa-pg -e POSTGRES_PASSWORD=<throwaway> -p 127.0.0.1:55432:5432 postgres:16`, then set `DATABASE_URL` for the process; remove with `docker rm -f shipmatch-qa-pg`.
- Git Bash on Windows rewrites arguments that start with `/`; set `MSYS_NO_PATHCONV=1` when passing URL paths through environment variables.

**C. Files**
`qa/QA_SESSION_01.md` … `13.md` (per-session detail, repro steps and evidence), `qa/QA_SUMMARY.md` (prioritised summary and fix status), this report `qa/QA_FULL_REPORT.md`.
