# ShipMatch QA — Session 01 (3 Oct 2026)

Exploratory, real-user testing in the in-app browser against `http://localhost:8000` (demo data, DEBUG on, SQLite).
Scope of this session: **find and list problems only — nothing was fixed.**

Accounts used: `admin` (Admin, Acme Imports demo org), `reviewer` (Reviewer), `owner@northwind-test.example` (Admin of the empty Northwind test org, from `local-test-accounts.txt`).
Viewports: pane default (~1024 px), 1440 px, 375 px mobile.

How to reuse this file: each session, flip rows in the coverage map, add new issues with the next `QA-0xx` id, and keep the "Test data left behind" list current.

---

## 1. Coverage map

Legend: ✅ tested · 🟡 partly tested · ⬜ not tested yet

| # | Area | Pages / flows | Status | Notes |
|---|------|---------------|--------|-------|
| 1 | Public / logged-out | `/account/login/` empty + wrong password, `/account/password/reset/`, 404, `/api/docs`, `/health/`, protected URLs redirect | ✅ | `/try/` and `/signup/` return 404 (see QA-016). Reset-email delivery itself not tested. |
| 2 | Sign-in security | Password reset *email*, 2FA setup/disable/recovery, password change, lockout/rate limit, logout | ⬜ | Only the pages load (checked). Nothing submitted. |
| 3 | Dashboard | Range tabs 7/30/90, KPI cards, unpaid ageing, issue bars, charts, free-time list, waiting-longest, "Check payments now", "Show overdue" | 🟡 | Looked at before it broke (QA-001). Buttons not clicked. Numbers identical across ranges because all seed data is from today. |
| 4 | Review queue | Tabs, filters, search, sort, assignee chips, pagination, export menu | 🟡 | Search and tab counts checked. Bulk approve/assign/post **not** run. |
| 5 | Shipment detail | Header, blockers, issues, accept/override, disputes box, shared invoices, field editing, PDF viewer, move/type, comments, activity, landed cost | 🟡 | SHP-000041/42/44. Inline edit, comments, PDF viewer, server-side approve guard tested. **Approve, reject, reopen, post, accept/override, confirm split, move document not clicked.** |
| 6 | Documents | List, tabs, document detail (unreadable doc), upload edge cases | 🟡 | Upload edge cases run on Northwind. Real-document upload happy path, set type, "Assign" to shipment not run. |
| 7 | Global search + shortcuts | `/` search, `?` overlay, `j/k/x/a/r/e/g` shortcuts | 🟡 | Overlay opens. Search results page loaded. Per-page shortcuts not exercised. |
| 8 | Rates | Quotes list, new quote form validation, extras, charge names, rules, CSV import (+ template/problems), export | 🟡 | Validation of new-quote form and CSV import checked (good). Create/edit/archive/delete real quote, extras CRUD, charge-name teaching, "Check open shipments now" not run. |
| 9 | Savings / ROI | `/savings/`, `/roi/` | ⬜ | `/savings/` is broken by QA-001 so not testable; `/roi/` loaded in the smoke crawl only. |
| 10 | Landed cost | Report, period filters, search, settings, CSV/Excel export, per-shipment split | 🟡 | Report + date edge cases checked. Split/confirm/reset on a shared invoice and per-shipment method not run. |
| 11 | Disputes | List (empty), settings | 🟡 | List/settings read. Creating, sending, replying, crediting, closing a dispute **not tested**. |
| 12 | Month-end | Accruals (period picker, other date), vendor statements, statement upload, month-end settings, lock period, adjustments, payments | 🟡 | Read-only. Lock/adjust/statement upload/payment add not run. |
| 13 | Reading accuracy | `/reports/accuracy/` | ✅ | Looks fine on seed data. |
| 14 | Audit log | List, filters, CSV export | 🟡 | List read; filters and export not exercised in the UI. |
| 15 | Team | List, invite validation, role table | 🟡 | Invite validation only. Role/limit change, remove, reset 2FA, password link not run. |
| 16 | Settings — Organization | Name/currency/zone, maker-checker, MFA, FX rates | 🟡 | Read only; no save. |
| 17 | Settings — API keys | Create/revoke key, use key against API | 🟡 | API auth checked with no/bogus key. A real key was not created. |
| 18 | Settings — Alerts / Email intake / Webhooks / Learning / Billing / Assignment / Customs | Pages and webhook URL validation | 🟡 | Webhook validation checked (excellent). Channel create/test, IMAP/Microsoft connect, webhook delivery, assignment rules, customs dates not run. |
| 19 | Integrations | QuickBooks/Xero connect, callbacks, post bill, payment check | ⬜ | Not tested (needs sandbox). |
| 20 | Access control | Reviewer vs Admin: pages and POST actions; tenant isolation (Northwind → Acme IDs) | ✅ | All good (see "What worked"). |
| 21 | Public API | Auth, 404/422 handling, OpenAPI spec, docs page | ✅ | Docs page broken (QA-004). |
| 22 | Notifications / My work / Clients | Pages | 🟡 | `/notifications/`, `/my-work/` read; `/clients/` broken by QA-001; create-client not run. |
| 23 | Workflow links | Emailed approval link `/approve/<token>/`, quick approve | ⬜ | Not tested. |
| 24 | Responsive | 375 px: review queue, shipment, rates, month-end, landed, team | 🟡 | Shipment detail overflows (QA-006). Remaining pages not checked. |
| 25 | Accessibility / dark mode / other browsers | — | ⬜ | Not tested. |

---

## 2. Issues found

Severity: **Critical** = data/availability loss · **High** = wrong results or security exposure · **Medium** = clear defect, workaround exists · **Low** = polish / copy / edge · **Verify** = might be intended, needs a decision.

### Critical

**QA-001 — Editing an invoice field with a huge number takes the whole app down for everyone**
- Where: shipment page → commercial invoice → *Total amount* (POST `/review/documents/<id>/field/`).
- Repro: on any shipment's commercial invoice, set Total amount to `99999999999999999999`.
- Result: "Total amount saved." The value flows into derived rows (`shipments_validationissue.amount_at_risk` and `rates_caughtcharge.amount_caught` became 9.99e19). After that, every page that reads those tables returns **HTTP 500 `decimal.InvalidOperation`**: `/dashboard/` (also the post-login landing page, so every user lands on a 500), `/savings/`, `/clients/`, the shipment page and the document page.
- Related: the same field also accepts `abc`, `-5` and `1e999` with a green "saved" message (no numeric validation), and the follow-up requests then 500.
- Why it matters: one typo by a reviewer can brick a company's dashboard.

### High

**QA-002 — Python traceback and server file path shown to users**
- Where: Documents list and document detail for a file that is not a real PDF (e.g. text saved as `.pdf`).
- Result: the "couldn't be processed" message contains `No /Root object! … Traceback (most recent call last): File "C:\path\to\shipmatch\.venv\Lib\site-packages\pdfplumber\pdf.py" …`. Leaks internals and looks broken.

**QA-003 — False "Container not on the bill of lading" errors on the primary shipment of a shared invoice**
- Where: SHP-000041 (shared freight invoice HL-880075 also covers SHP-000042 and SHP-000043).
- Result: 6 blocking *errors* (3 on the freight invoice, 3 on the credit note) for containers that belong to the sibling shipments the system itself identified. SHP-000042 (the other side of the same invoice) shows only the "check the split" warning. The errors block approval until an approver overrides them.

### Medium

**QA-004 — API reference page (`/api/docs`) cannot render**
- Swagger UI shows "Unable to render this definition"; console: `YAMLException: duplicated mapping key (1:8325)`. The spec (`/api/openapi.json`) has a response key defined twice around the `/api/{org}/exports/{kind}` operation (a `"200"` entry appears twice). The sidebar link "API reference" leads to this page.

**QA-005 — Raw field keys and junk values in the activity/audit trail**
- Activity on a shipment reads "admin changed **issue_date** from 2026-01-08 to not-a-date" (raw key instead of "Issue date"). Issue date accepted `not-a-date` with "Issue date saved." (no date validation).
- Vendor learning then **learned from the bad edits** (Settings → Vendor learning shows corrections `12,5.5.5`, `1e999`, `9999…`), polluting future reads for that vendor.

**QA-006 — Mobile (375 px): shipment page scrolls sideways**
- The shared-invoice split table (`table.lc-split`) pushes the page to 415 px wide. Other tables scroll inside their own wrapper; this one does not.

**QA-007 — "Show PDF" does nothing visible at ~1024 px and below**
- At 1440 px the PDF opens in a side panel. At the default pane width the viewer is stacked at the very bottom of the page (≈4,700 px down). Clicking "Show PDF" gives no scroll or feedback, so it looks broken.

### Low

- **QA-008** Dashboard "Waiting longest" table: the *Waiting* column is clipped ("2 hours, 54 minut…") at ~1024 px.
- **QA-009** Month-end: a future period (e.g. 31 Jan 2030, via the "Other date" box) is accepted and shows "Lock version 1".
- **QA-010** Landed cost: an inverted range (from 1 Dec 2026, to 1 Jan 2026) is silently swapped; garbage dates silently fall back to "Last 12 months". No message either way.
- **QA-011** Landed cost per-unit precision is inconsistent: `45.548`, `7.3744`, `4.7175`, `17.90`, `5.60`.
- **QA-012** Time-zone picker lists 519 entries including deprecated aliases (`Asia/Calcutta`, `America/Buenos_Aires`, `Asia/Katmandu` …). Organization zone shows UTC while the footer shows Asia/Karachi (user zone) — fine, but easy to misread.
- **QA-013** Developer wording in user-facing copy: Document page says "turn on OCR (OCR_PROVIDER=textract)"; Email intake mentions `INBOUND_EMAIL_DOMAIN`, `MS_CLIENT_ID`; Billing mentions `BILLING_ENABLED`.
- **QA-014** Comment box: typing `@Sam` and not picking from the suggestion list posts plain text and (apparently) notifies nobody; no hint that it didn't work.
- **QA-015** Upload message for a content-identical file names the *new* file: "…éééé.pdf was uploaded before" although that name was never uploaded (it matched `passwd.pdf` by content).
- **QA-017** `/api/…` with a bogus `Bearer` key **and** a logged-in session cookie returns 200 (session wins over the invalid key). Without a cookie it correctly returns 401.
- **QA-018** Native "Choose Files / No file chosen" input is unstyled next to the otherwise polished upload card (review queue, mobile and desktop).
- **QA-019** Documents "Not in a shipment" shows `S11_3_freight_invoice.pdf` twice (docs 97 and 180, received 35 min apart), so duplicates of unreadable files aren't caught.

### Verify (may be intended)

- **QA-016** `/try/` and `/signup/` return 404 and the login page links to neither. Confirm these are feature-flagged off (demo mode / billing off).
- **QA-020** `admin` (org Admin) sees a **"Platform admin"** link and `/admin/` opens. README says `/admin/` is only for the admin when seeded with `--superuser`. Reviewer is correctly redirected. Confirm against the in-progress platform-admin work (`tests/test_platform_admin.py`).
- **QA-021** Dashboard shows "8 posted to accounting" but also "17 posted bills not checked yet".
- **QA-022** SHP-000041 "Payable total USD 38,935.00" = commercial invoice 28,440 + the **whole** shared freight invoice 10,615 − credit 120, while this shipment's share of that invoice is 2,522.50 (landed cost uses the share). Confirm which one approval limits should use.
- **QA-023** Browser 404 shows Django's DEBUG URL list (expected in dev only — make sure `DEBUG=False` in deployed environments).

---

## 3. What worked well (don't re-test unless code changes)

- Login/logout, wrong-password message, protected URLs redirect to login.
- New-quote form: server-side errors for empty vendor, 8-char currency, end-before-start date, negative amount.
- Rates CSV import: whole-file validation, row-numbered problems, nothing imported on error.
- Webhook URLs: rejects http, localhost, 127.0.0.1, 169.254.169.254, 10.x, `javascript:`, non-standard ports.
- Team invite: email validated before anything is created.
- Role enforcement: Reviewer gets 403 on every admin page and on approve/reject/post/bulk/lock/remove POSTs; nav hides those links.
- Tenant isolation: Northwind owner gets 404 for Acme shipments, documents, files, rates, disputes, statements, webhooks; search returns nothing from Acme.
- Server-side approve guard: approving a shipment with open errors via direct POST leaves it in "Needs review".
- Comments: HTML is escaped (`<img onerror>` shown as text); @-mention autocomplete works.
- Uploads: empty file, renamed non-PDF, `.html`, corrupt ZIP, path-traversal filename all handled with clear messages.
- API: no key / bad key → 401, bad path params → 422 with details, CSRF enforced for session POST.
- Month-end maths ties out (accrual = received + estimated; journal debits = credit).
- Mobile review queue layout.

---

## 4. Test data left behind / state to know about

Acme Imports (demo) — **needs attention before the next session**
- **REPAIRED on 4 Oct 2026** (user-approved): the two rows below were set to 0 and shipment 41 revalidated; Dashboard, Savings, Clients, SHP-000041 and document 160 return 200 again. A pre-repair backup of `db.sqlite3` is in the Windows temp folder (`db.sqlite3.bak-before-repair`). Original description of the damage follows.
- (was) Dashboard, Savings, Clients, shipment SHP-000041 and document 160 returned 500 because of QA-001. Cause: two derived rows hold out-of-range amounts — `shipments_validationissue` rowid 736 (`amount_at_risk`) and `rates_caughtcharge` rowid 17 (`amount_caught`, `amount_latest`). I tried to reset them and re-run validation for shipment 41, but the action was blocked by the permission classifier, so **they are still broken**. Re-running `seed_demo` / restoring `db.sqlite3` from a backup, or setting those rows to 0 and calling `validate_shipment(41)`, will fix it.
- Document 160 *Total amount* was put back to `28440.00`; document 159 *Issue date* was put back to `2026-01-08`. The audit log still shows the junk edits.
- Vendor learning for **Apex Housewares Manufacturing Ltd.** learned from the junk edits → use *Forget this vendor* (Settings → Vendor learning).
- A QA comment (`#comment-1`, with HTML text) was added to SHP-000042.

Northwind Traders (test)
- One junk upload `passwd.pdf` (document 250, "Couldn't read"). No quotes or other records created.

Browser
- Left signed in as `admin`.

---

## 5. Suggested plan for the next sessions

1. **Session 02 — Core approval loop** (after data is repaired): clean shipment → fix a value → accept warning → override error as Approver → approve → post (QuickBooks sandbox) → payment check; reject + reopen; bulk approve/assign; maker-checker (same user can't approve own edit); approval-limit boundary (e.g. USD 50,000 for Maria); quick-approve email link `/approve/<token>/`.
2. **Session 03 — Disputes & money**: create a dispute from a shipment, edit, send (check email), reply, follow-up date, credit, resolve/close, release; month-end lock/unlock/adjustments; statement upload (PDF/CSV/XLSX) and findings; payments.
3. **Session 04 — Intake & integrations**: real PDF/image/XLSX/ZIP uploads, duplicate handling, scanned docs, mailbox IMAP form validation, forwarding address, Microsoft connect screens, QuickBooks/Xero connect/disconnect/reconnect, webhooks create/test/replay/rotate, API key create/revoke and real API calls.
4. **Session 05 — Accounts & admin**: password reset email, password change rules, 2FA setup/recovery/login, team role/limit changes, remove member, reset 2FA, org settings save, assignment rules, customs/free-time dates, learning settings, client portfolio + create client, platform admin.
5. **Session 06 — Cross-cutting**: all pages at 375/768 px, keyboard-only use and focus order, screen-reader labels (two "CSV/Excel" pairs in the export menu), dark mode if any, Firefox/Safari, large data volumes (pagination at 500+ shipments), slow/failed network behaviour.

Re-run the smoke crawl each session: every GET page from the URL lists should return 200/302/403 — never 500.
