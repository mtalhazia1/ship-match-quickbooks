# ShipMatch QA — prioritised summary of sessions 01–09

Period: 3–4 Oct 2026. Method: exploratory testing in the in-app browser, issues listed only (nothing was fixed), against the local demo data plus the empty **Northwind Traders (test)** org for destructive tests. Detail, repro steps and evidence are in the per-session logs:
[01 platform sweep](QA_SESSION_01.md) · [02 approval loop](QA_SESSION_02.md) · [03 disputes & month-end](QA_SESSION_03.md) · [04 intake & integrations](QA_SESSION_04.md) · [05 accounts & admin](QA_SESSION_05.md) · [06 cross-cutting](QA_SESSION_06.md) · [07 gap closing](QA_SESSION_07.md) · [08 webhooks, accounting, DEBUG off](QA_SESSION_08.md) · [09 static files, re-check, tenant sweep](QA_SESSION_09.md)

**Totals:** 73 numbered issues (QA-001 … QA-074; QA-047 was never used). 2 Critical/High-security, 5 High/Medium-high, 22 Medium, 36 Low, 8 "verify / decide". Session 07 added QA-065 … QA-074 and widened QA-002, QA-003, QA-010, QA-025. Several were re-explained or downgraded later; the table below uses the latest understanding.

**Session 09 (7 Oct 2026) added one open issue:** QA-078 (Low: static files are cached for 60 seconds and not content-hashed; refines QA-063). It also re-checked the earlier fixes in the browser (all held) and ran search-input and cross-tenant sweeps with no findings.

---

## Fix status (updated 7 Oct 2026)

Fixes are in commits `0aea335`, `0d04083` and `fdbe801` (earlier branch) and `fe46865` (`fix/qa-remaining`).

- **Fixed:** QA-001, 002, 003, 004, 005, 006*, 007*, 008, 009, 010, 011, 012, 013, 014, 015, 017, 018, 024, 025, 026, 028, 029, 030, 032, 033, 034, 035, 036, 038, 039, 041, 042, 043, 044, 046, 048, 049, 051, 053, 054, 055, 058, 060, 061, 062, 064, 065, 066, 067, 068, 069, 071, 072, 074.
  (QA-006 and QA-007 were checked in the browser at 375 px and 1024 px on SHP-000041 and SHP-000044.)
- **Session 08:** QA-075 and QA-076 fixed on branch `fix/qa-session-08`; QA-077 fixed too (the API docs page now uses the Swagger UI files bundled with django-ninja, under the normal Content-Security-Policy).
- **Partly fixed:** QA-056 (totals are paged; about 5 queries per ready shipment remain).
- **Not a defect on inspection:** QA-019 (different bytes, not duplicates), QA-037 and QA-052 (messages exist and render), QA-045, QA-070.
- **Left open, performance or deployment:** QA-057 (pdf.js loads eagerly; the evidence highlighting depends on it), QA-063 (static file caching), QA-031 (redirect URI comes from `.env`).
- **Left open, product decisions:** QA-016, 020, 022, 023, 027, 040, 050, 059, 073.

---

**Session 08 (7 Oct 2026) added three open issues:** QA-075 (Medium: QuickBooks default expense account accepts any value and says "saved"), QA-076 (Low: a long webhook host name was echoed in full in the error), QA-077 (Low, verify: API docs page loads Swagger UI from public CDNs).

---

## 1. Do these first

| # | Issue | Why it's first | Rough effort |
|---|---|---|---|
| **QA-048** | **An org admin can add any existing user to their company by email, then generate a password-set link (shown on screen) or reset 2FA for that shared account.** | Cross-tenant account takeover path for anyone whose email is known, including admins of other companies. Also leaks which emails have accounts. Confirmed in session 05 (link generated, not used). | M — require the invitee to accept; don't reveal that the account exists; limit "password link" / "reset 2FA" to accounts that belong only to the acting org. |
| **QA-001** | **Free text and out-of-range numbers are accepted in extracted fields (`abc`, `-5`, `1e999`, a 20-digit total).** The huge value propagated into derived rows and then every page reading them returned HTTP 500: Dashboard (and the post-login landing page for everyone), Savings, Clients, the shipment and document pages. | One reviewer typo can take a company's dashboard down. (Data was repaired afterwards.) The audit log and vendor learning also store the junk (QA-005). | M — validate/normalise per field type on save (amounts, dates, lists), cap magnitude, and make the readers tolerate bad rows. |

## 2. High / Medium-high — fix next

| # | Issue | Impact | Effort |
|---|---|---|---|
| QA-002 + QA-043 | Python **traceback and server file path shown to users** on unreadable documents (including **password-protected PDFs**, a common real case); root cause: any file that merely starts with `%PDF` is accepted (web and API). | Information leak, looks broken. | S — friendly message; validate PDF structure at upload. |
| QA-003 | "Container not on the bill of lading" errors on the primary shipment of a shared invoice (SHP-000041: 6 blocking errors). **Session 07: they clear automatically when the split is confirmed**, but nothing on the page says so. | Looks like false positives, invites unnecessary overrides. | S (explain on the page) |
| **QA-074** | **Parallel identical uploads return HTTP 500** (unhandled unique-constraint race on the file hash). Realistic via double-click or email + upload together. | Server error on a normal action; data stays safe. | S |
| QA-041 | IMAP form **saves the mailbox (with password) even when the connection test fails**; scheme-prefixed hosts accepted; local hosts rejected inconsistently. | Broken, credential-holding rows; unnecessary DNS lookups. | S |
| QA-032, QA-033 | Month-end: a period can be **locked on its own last day**, and a **malformed or empty `period` silently locks the latest month-end** (locks are permanent). | Irreversible audit snapshots from a wrong click or request. | S |
| QA-054, QA-055 | Stale pages show Django's raw CSRF error; after session expiry the login `next` points at a POST-only URL, so the user lands on a **blank 405 page**. | Everyday failure path looks broken. | S |
| QA-056 | **`/my-work/` takes ≈ 2 s** for 110 items (everything else 50–250 ms). | Gets worse with team size. | S–M (per-row queries). |
| QA-035 | Payment currency `DOLLARS` silently truncated to `DOL`; other forms reject it. | Junk financial data. | S |
| QA-004 | **API reference page does not render** (duplicated key in the OpenAPI spec, likely a repeated `"200"` response). | Public docs unusable. | S |
| QA-024, QA-025 | Shipment shows **"Ready to approve" but cannot be approved** (split unconfirmed); **Approve button approves in one click** while the shortcut says a confirmation opens first. | Confusing; accidental approvals. | S |

## 3. Medium — plan into the backlog

| # | Issue |
|---|---|
| QA-065, QA-067, QA-068 | Disputes accept an amount of 0 or above the invoice total; a credit above the dispute is recorded and inflates "Recovered" (USD 9,999 on a USD 525 dispute); quotes accept 0 amounts, duplicate charge codes, 1e9 rates and endless identical quotes/extras. |
| QA-005 | Activity/audit show raw keys (`issue_date`); vendor learning learns from bad edits. |
| QA-006 + QA-007 + QA-057 | Shipment page on small screens: sideways scroll (landed-cost radio group, legend row, split table), "Show PDF" gives no feedback because the viewer is ~4,700 px down, and ~1.8 MB of pdf.js loads eagerly. |
| QA-022 | Payable total includes the **whole** shared invoice (USD 38,935) while the shipment's share is 2,522.50. Decide which one approval limits use. |
| QA-029 | Confirming a split silently accepts siblings' warnings under your name (and can block you by maker-checker). |
| QA-030 | A partly posted shipment (2 of 3 bills) stays "Approved" with no header summary. |
| QA-040 | Shipment numbers are one **global** sequence (reveals other tenants' volume). |
| QA-036 | Statement rows that can't be read are dropped with no warning. |
| QA-051 | No server-side length limit (300-char org name saved; would be a 500 on PostgreSQL). |
| QA-020 | Seeded demo `admin` is a Django superuser (README says only with `--superuser`). Decide if intended. |

## 4. Low — batch into polish passes

- **Validation & messages:** QA-010 (landed date ranges silently swapped), QA-011 (per-unit decimals vary), QA-038 (0 vs −5 error text), QA-042 (API accepts `status=bogus`, `limit=-1`), QA-046 (password can be changed to itself), QA-064 (`page=0` shows last page), QA-014 (`@Sam` without picking does nothing), QA-037 / QA-052 (no visible confirmation after some actions).
- **Copy:** QA-013 + QA-044 (env-var names in user messages), QA-045 (nested ZIP message), QA-015 (duplicate message names the wrong file), QA-028 (mixed-currency totals run together), QA-039 (dispute "To" empty).
- **Audit trail:** QA-026 (`SimpleLazyObject 3` in Record column), QA-027 (denied actions not logged), QA-034 ("Lock version 2" offered when nothing changed).
- **Accessibility:** QA-053 (errors use `role=status`), QA-058 (muted text 4.28:1), QA-059 (no dark mode / forced colours, px font sizes), QA-060 (skip link doesn't move focus), QA-061 (tap targets < 24 px), QA-062 (heading skips, header-less tables, repeated titles), QA-049 (recovery codes run together).
- **Session 07 polish:** QA-066 (credit-note picker lists every document), QA-069 (extras edit says "Nothing changed" though saved), QA-070 (landed method can't return to org default), QA-071 (goods lines labelled "Freight accrual" in the journal), QA-072 (wrong reason for ZIP bomb), QA-073 (text-light PDF treated as scan, verify).
- **Misc:** QA-008 (dashboard "Waiting" column clipped at ~1024 px), QA-009 (month-end "Lock" button shown for future periods; server refuses), QA-012 (519 time zones incl. deprecated aliases), QA-017 (bogus Bearer key + live session returns 200), QA-018 (unstyled native file input), QA-019 (duplicate unreadable docs), QA-063 (static files uncached/uncompressed on dev server).

## 5. Needs a product decision ("verify")

QA-016 (`/try/` and `/signup/` return 404: intended?), QA-020, QA-022, QA-023 (Django DEBUG 404 page, make sure `DEBUG=False` in deployments), QA-025, QA-027, QA-029, QA-050 (anyone can lock out a known user for 15 minutes; in-memory counters), QA-031 (QuickBooks redirect URI pinned to `:8000`).

Already explained: **QA-021** (8 posted vs 17 unchecked was just never-polled bills).

---

## 6. Suggested fix order

1. **Week 1 — safety:** QA-048, QA-001 (+QA-005, QA-043 root cause), QA-002, QA-033/032 (month-end locks).
2. **Week 2 — trust in the core loop:** QA-003, QA-024/025, QA-022/029, QA-041, QA-035/036, QA-054/055.
3. **Week 3 — platform polish:** QA-004 (API docs), QA-056, QA-006/007/057 (shipment page), message/copy clean-ups.
4. **Ongoing:** accessibility batch (QA-053, 058–062) and the "decide" list.

**Common root causes (fix once, close many):**
- *Input validation at the boundary* → QA-001, 005, 035, 036, 042, 043, 051, 033, 010, 065, 067, 068.
- *Error surfaces designed for developers* → QA-002, 013, 044, 054, 055, 045.
- *Tenant boundary assumptions* → QA-048, 040, 020, 041.
- *Approval state vs. UI wording* → QA-003, 022, 024, 025, 029.

## 7. What is already solid (don't spend time here)

Role-based access (every admin page and POST returns 403 for reviewers); tenant isolation of records (other-company ids → 404); maker-checker, approval limits (exact to the cent), locked/posted shipment protections; webhook URL validation (rejects http, localhost, private ranges); rates CSV import and new-quote validation; sign-in, password rules, lockout, 2FA (replay, attempt limit, single-use recovery codes, org-wide policy); API keys (hashed, scoped, revocable, clear scope errors); QuickBooks posting is idempotent and shows clear failures; month-end versions are immutable with a stale-lock guard; security headers and CSP; keyboard operation and focus handling; performance at 600+ shipments apart from `/my-work/`.

## 8. Coverage and gaps

| Area | Sessions | Status |
|---|---|---|
| Public pages, login, API docs | 01, 05 | ✅ |
| Dashboard, queue, shipment detail, documents | 01, 02, 04 | ✅ (approve/reject/override/post/bulk done) |
| Rates, savings, landed cost | 01, 07 | ✅ quotes/extras/charge names CRUD, CSV import, splits, methods, savings/ROI |
| Disputes | 03, 07 | ✅ full lifecycle incl. send (console email), reply, credit, resolve, close, release, discard, roles |
| Month-end, statements, payments | 03, 07 | ✅ incl. PDF/XLSX statements, adjustments, journal Excel |
| Intake, uploads, mailboxes, API | 04, 07 | 🟡 uploads, limits, formats, concurrency, API keys, webhook management done; webhook delivery, real IMAP, Microsoft/Gmail not run |
| Integrations (QuickBooks, Xero) | 02, 04 | 🟡 QuickBooks post + payment read done; connect/disconnect and Xero not run |
| Accounts, 2FA, team, org settings, notifications, learning, approval links, shortcuts | 05, 07 | ✅ |
| Responsive, accessibility, performance, headers | 06 | ✅ Chromium only |
| Other browsers, real screen reader, offline, concurrency, `DEBUG=False`/PostgreSQL | — | ⬜ not tested |

## 9. Re-test checklist after fixes

1. **QA-048:** invite an email that already exists elsewhere; confirm a consent step, a neutral message, and no password link / 2FA reset on a shared account.
2. **QA-001:** set Total amount to `abc`, `-5`, `1e999`, `99999999999999999999` on a commercial invoice; expect a field error and no 500 anywhere.
3. **QA-033/032:** POST `period=not-a-date` and today's date to the lock action; expect refusal.
4. **QA-002/043:** upload `x.pdf` containing text; expect a friendly message and no traceback in Documents.
5. **QA-003:** SHP-000041 should have no container errors from its own shared invoice.
6. Repeat the audit scripts from session 06 (HTML/a11y audit, contrast scan, 375/768 sweep) and the session 01 smoke crawl (every GET page 200/302/403, never 500).

## 10. Environment notes for whoever picks this up

- Port 8000 on the test machine was serving a different project; ShipMatch was run with `manage.py runserver 8001 --noreload`.
- Restore points taken before each session: `%TEMP%\db.sqlite3.bak-session0N-start` (and extra ones before posting and before repairs). The database currently reflects the end of session 06 (approved/posted shipments from session 02, Northwind test data); the permanent month-end locks created in session 03 were rolled back.
- Real Intuit sandbox bills were created in session 02 (SHP-000021 partial, 027, 045, 046); they can't be removed by restoring the database.
- Test accounts: demo `admin`/`reviewer`/`approver` (README) and the Northwind owner (`local-test-accounts.txt`). Two stray user accounts (`qa-reviewer@example.com`, `qa-approver@example.com`) with no company remain from session 05.
