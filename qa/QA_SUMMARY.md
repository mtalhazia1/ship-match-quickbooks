# ShipMatch QA — prioritised summary of sessions 01–21

Period: 3–4 Oct 2026. Method: exploratory testing in the in-app browser, issues listed only (nothing was fixed), against the local demo data plus the empty **Northwind Traders (test)** org for destructive tests. Detail, repro steps and evidence are in the per-session logs:
[01 platform sweep](QA_SESSION_01.md) · [02 approval loop](QA_SESSION_02.md) · [03 disputes & month-end](QA_SESSION_03.md) · [04 intake & integrations](QA_SESSION_04.md) · [05 accounts & admin](QA_SESSION_05.md) · [06 cross-cutting](QA_SESSION_06.md) · [07 gap closing](QA_SESSION_07.md) · [08 webhooks, accounting, DEBUG off](QA_SESSION_08.md) · [09 static files, re-check, tenant sweep](QA_SESSION_09.md) · [10 concurrency](QA_SESSION_10.md) · [11 exports, reset, caching](QA_SESSION_11.md) · [12 PostgreSQL](QA_SESSION_12.md) · [13 scale](QA_SESSION_13.md) · [18 background workers](QA_SESSION_18.md) · [19 production rehearsal](QA_SESSION_19.md) · [20 load on PostgreSQL](QA_SESSION_20.md) · [21 security deep-dive](QA_SESSION_21.md)

Sessions 14–17 (planned: webhook delivery to a real receiver, a real mailbox, QuickBooks/Xero OAuth) have not been run.

**Totals:** 73 numbered issues (QA-001 … QA-074; QA-047 was never used). 2 Critical/High-security, 5 High/Medium-high, 22 Medium, 36 Low, 8 "verify / decide". Session 07 added QA-065 … QA-074 and widened QA-002, QA-003, QA-010, QA-025. Several were re-explained or downgraded later; the table below uses the latest understanding.

**Sessions 18–21 (16–17 Oct 2026, run in the order 18, 21, 19, 20) added 15 issues, QA-091 … QA-105.** First runs with real Celery workers and Redis (18), a role-by-route security matrix of 2,280 web requests and 119 API cases (21: no isolation or permission gaps), the production Docker stack behind Caddy with a backup/restore drill (19) and 25 concurrent users on PostgreSQL (20). All 15 are fixed on branch `fix/qa-sessions-18-21` (PR #3; see Fix status):

| # | Severity | Issue | Status |
|---|---|---|---|
| QA-101 | High | Production started with the public default `DJANGO_SECRET_KEY` when the key was missing | **Fixed 19 Oct** |
| QA-091 | Medium/High | A document uploaded while the broker was down returned 500, was never read and could not be re-sent | **Fixed 19 Oct** |
| QA-102 | Medium | `local-test-accounts.txt` (with a password) was copied into the production image | **Fixed 19 Oct** |
| QA-092 | Medium | A document whose worker died mid-read stays "Extracted" for an hour with a misleading message | **Fixed 24 Oct** (the sweep re-reads it after 15 min; the page says it is being retried). Visibility timeout left at 1 h on purpose: webhook retries are scheduled up to 6 h ahead |
| QA-093 | Medium | Production Redis has no persistence: queued work is lost on a crash | **Fixed 24 Oct** |
| QA-094 | Medium | With the cache down, sign-in and every API call return 500 | **Fixed 24 Oct** |
| QA-097 | Medium | Password reset has no rate limit (inbox flooding) | **Fixed 24 Oct** |
| QA-098 | Medium | Small hostile PDFs (20,000 pages, deep nesting) hold a worker; no page limit, no task time limit, OCR on every page | **Fixed 24 Oct** |
| QA-105 | Medium | gunicorn runs a fixed 3 sync workers: 25 busy users wait 3–5 s per page | **Fixed 24 Oct** |
| QA-103 | Low/Medium | Caddy's 30 MB body limit is below the app's ZIP (100 MB) and email (40 MB) limits: bare 413 | **Fixed 24 Oct** |
| QA-095 | Low | "Post bills" and "Check payments now" return 500 when the broker is down | **Fixed 24 Oct** |
| QA-096 | Low | Tasks for rows deleted while queued fail with `DoesNotExist` | **Fixed 24 Oct** |
| QA-099 | Low | A statement whose stored file is missing gives 500 | **Fixed 24 Oct** |
| QA-100 | Low | Dependencies are not pinned; no lock file; test tools in the production image | **Fixed 24 Oct** |
| QA-104 | Low | `deploy/backup.sh` doesn't back up the PDFs in the default setup | **Fixed 24 Oct** |

**Session 13 continued (12 Oct 2026, same data set, scratch copy of the database):** the fixes hold (My work 0.34 s, month-end page 2.2 s, API documents list 9 queries). 30 more pages were timed with no issues. One new issue: **QA-090** (Medium on SQLite, fixed the same day): the Savings CSV export returns HTTP 500 once the period holds about 2,000 catches. It is a regression from the QA-089 fix: a prefetch over every catch exceeds SQLite's expression-depth limit, and the code before `ebff5e8` returned the file in 0.76 s. The slowest downloads are the documents Excel (17.8 s) and the month-end journal (5–10 s).

**Session 13 (8 Oct 2026, 3,000 synthetic shipments) found five speed issues; four are fixed on branch `fix/qa-session-13`, one was withdrawn:** QA-085 (month-end report 16 s and 6.3 MB: now 5.8 s and 143 KB, lines paged, build faster; its Excel journal is still slow: `openpyxl` needs about 8 s to save 127,000 cells, and installing `lxml` did not help), QA-086 (My work 9.4 s and 1,984 queries: now 0.6–0.9 s and 193 queries), QA-087 (landed-cost report: withdrawn, a one-time catch-up caused by the synthetic data), QA-088 (API documents list: 209 queries now 9; exports 3–4 s faster), QA-089 (dashboard and Savings about 30% faster). The paged lists (queue, documents, audit, disputes, rates, search) stay under half a second.

**Session 12 (8 Oct 2026, first run on PostgreSQL) added two issues, both fixed the same day:** QA-083 (the dispute settings page crashed on a reply-to address over 254 characters: SQLite does not enforce column lengths, Postgres does) and QA-084 (moving a document into a shipment that another request was emptying gave a 500 and could delete a shipment just after a document landed in it). All migrations, the demo build and the test suite (1,063 passing, one timing-flaky test, now made deterministic) work on Postgres; the row-lock fixes from session 10 hold on real row locks.

**Session 11 (8 Oct 2026) added one open issue:** QA-082 (Low, verify: signed-in pages and downloads send no `Cache-Control`; the file view allows 5 minutes of private caching). It also checked spreadsheet formula injection across 3 Excel files, 4 CSV exports and 3 API exports (all safe) and the password-reset flow (does not reveal which addresses have accounts).

**Session 10 (7 Oct 2026) added three open issues:** QA-079 (Medium on SQLite: concurrent writes return 500 "database is locked"), QA-080 (Medium: approving a shipment twice at once records two approvals and two audit events), QA-081 (Medium: approve and reject at the same moment are both recorded and the shipment ends approved). QA-080/081 were check-then-write races in `shipments.views.approve`/`reject`; all three are now fixed (row lock inside a transaction; SQLite waits for the write lock) and re-run in the browser.

**Session 09 (7 Oct 2026) added one open issue:** QA-078 (Low: static files were cached for 60 seconds and not content-hashed; refines QA-063), now fixed with hashed file names and an immutable cache header. It also re-checked the earlier fixes in the browser (all held) and ran search-input and cross-tenant sweeps with no findings.

---

## Fix status (updated 24 Oct 2026)

The fixes up to 7 Oct are in commits `0aea335`, `0d04083` and `fdbe801` (earlier branch) and `fe46865` (`fix/qa-remaining`). On 12 Oct the two lines of work were combined on `main` of `mtalhazia1/ship-match-quickbooks`: the 8–9 Oct fixes made there (`claude/optimistic-johnson-s7l5qg`, merged as PR #1 and #2) plus the session 08–13 fixes from `fix/qa-session-08` and `fix/qa-session-13`.

- **Fixed:** QA-001, 002, 003, 004, 005, 006*, 007*, 008, 009, 010, 011, 012, 013, 014, 015, 017, 018, 024, 025, 026, 028, 029, 030, 032, 033, 034, 035, 036, 038, 039, 041, 042, 043, 044, 046, 048, 049, 051, 053, 054, 055, 058, 060, 061, 062, 064, 065, 066, 067, 068, 069, 071, 072, 074.
  (QA-006 and QA-007 were checked in the browser at 375 px and 1024 px on SHP-000041 and SHP-000044.)
- **Fixed 8–9 Oct (`mtalhazia1` line):**
  - QA-056: approval checks on My work run in bulk (`prefetch_approval`; 40 ready shipments: 244 → 50 queries). This is the design kept when the two lines were combined; the session 13 version of the same fix (QA-086, `bulk_hints`) was dropped.
  - QA-057: PDF.js loads when the viewer nears the screen or a document is asked for (stacked/mobile layouts no longer download 1.8 MB up front).
  - QA-063: production uses hashed static names (`immutable`, 10-year cache, gzip); the same change as QA-078. Also fixed: the favicon URL was resolved at import, which would have stopped `migrate` on a fresh production container.
  - QA-073: little text counts as a scan only when a page is mostly image (or there is no text at all).
  - QA-050: a new password lifts a sign-in lockout (the lockout message tells people to reset it); system check `accounts.W001` warns when production runs with a per-process cache. Production already uses Redis.
  - QA-031: Microsoft, QuickBooks and Xero redirect URIs default to `SITE_URL` + path. Production now sets `SITE_URL`.
  - QA-020: a plain `seed_demo` already matches the README; `--superuser` now also promotes an existing admin.
  - QA-027: refused actions (web and API 403s, including API-key scope refusals) are written to the audit log, at most once a minute per person and URL.
- **Fixed in sessions 08–13:** QA-075, 076, 077, 078, 079, 080, 081, 083, 084, 085, 086, 088, 089 (details in each session log).
- **Fixed 12 Oct while combining:** My work for an approver **with an approval limit** still ran about 7 queries per ready shipment in both versions (3,000 shipments: 2,001 queries, 2.25 s). `Document.field()` ignored prefetched fields; it now reads them, and `prefetch_approval(with_totals=True)` loads the documents in bulk: 50 queries, 0.26 s.
- **Fixed 12 Oct:** QA-090 (Savings CSV export 500 once the catches span more than about 1,000 shipments on SQLite; the shipment prefetch now runs in batches of 500; 0.49 s at 3,000 shipments).
- **Fixed 13 Oct:** QA-082 (every response to a signed-in user or an API key now carries `Cache-Control: private, no-store`, unless the view sets its own; the original-file views keep `private, max-age=300` so the evidence viewer does not download a PDF twice).
- **Improved 13 Oct (rest of QA-085 and QA-088):** Excel files are now written with XlsxWriter instead of openpyxl (openpyxl still reads uploaded spreadsheets). At 3,000 shipments: documents Excel 15.7 → 6.3 s, month-end journal 7.8 → 4.6 s, shipments Excel 7.0 → 4.3 s, issues Excel 3.1 → 1.6 s, and the files are up to 60% smaller. All seven Excel downloads were compared cell by cell with the old output (values, types, number formats, fonts, fills, widths, frozen panes): identical.
- **Fixed 19 Oct (`fix/qa-sessions-18-21`):**
  - QA-101: with `DJANGO_DEBUG=0` the settings refuse to load (web, worker and beat) when `DJANGO_SECRET_KEY` is missing, the built-in default, `change-me` or `django-insecure-…`, or `FIELD_ENCRYPTION_KEY` is missing or not a Fernet key.
  - QA-091: queuing a document never fails the upload (a broker error is logged and the document stays "received"); uploading an unread document again re-queues it after 2 minutes; a beat task (`requeue_stalled_documents`, every 5 minutes) re-queues documents left "received" or "extracted" for 15 minutes, at most 3 times, then marks them as errors. While the broker is down it waits without using up attempts. Also covers QA-092's stuck documents.
  - QA-102: `.dockerignore` leaves out `local-test-accounts.txt`, `.env.*`, `tests/` and `qa/`.
- **Fixed 24 Oct (`fix/qa-sessions-18-21`):**
  - QA-092: a document stuck mid-read says it is being retried instead of "no bill of lading … was found" (the QA-091 sweep re-reads it). The broker's visibility timeout is left at its 1-hour default on purpose: webhook retries are scheduled up to 6 hours ahead, and a shorter timeout would deliver them twice.
  - QA-093: Redis writes an append-only file to a `redisdata` volume, so queued tasks survive a crash or a re-created container.
  - QA-094: with the cache unreachable, sign-in, 2FA, the API rate limit and the plan-usage banner carry on without their counters (logged) instead of returning 500. Chosen: fail open, as the password and the 2FA code are still checked.
  - QA-095: "Post bills" (single and bulk) and "Check payments now" say "Couldn't start it … try again in a minute" when the broker is down; nothing is recorded.
  - QA-096: document and posting tasks for rows deleted while queued return "skipped".
  - QA-097: password-reset emails are limited to 3 per address and 20 per IP per hour (`PASSWORD_RESET_PER_EMAIL_PER_HOUR`, `PASSWORD_RESET_PER_IP_PER_HOUR`); extra requests get the same page and no email.
  - QA-098: PDFs over `INTAKE_MAX_PAGES` (500) are refused at intake with a clear message; reading a document stops after `DOCUMENT_TASK_TIME_LIMIT` (300 s, prefork workers) with a plain-language error; OCR and the PDF sent to the AI cover at most `OCR_MAX_PAGES` (50) pages.
  - QA-099: downloading a document, original or statement whose stored file is gone answers 404 "missing from storage"; a statement's file is deleted only after its row is committed.
  - QA-100: `requirements.lock` (production, hash-checked, installed with `--require-hashes --no-deps`) and `requirements-dev.lock` (the same versions plus pytest, ruff and pip-audit; used by the quick start). pytest is no longer in the image. `pip-audit` over the lock found three pypdf CVEs in 6.21.1; the minimum is now 6.21.2 and both locks audit clean. The local venv was moved to the locked versions.
  - QA-103: Caddy accepts request bodies up to 110 MB (the app's largest limit is 100 MB ZIPs), so oversized files get the app's message.
  - QA-104: `deploy/backup.sh` now dumps the database and tars `/app/media` through the production compose files, like `deploy-droplet.sh backup`, and is executable.
  - QA-105: gunicorn reads `config/gunicorn.conf.py`: 2 × CPUs + 1 processes (at most 9), 4 threads each (`gthread`), overridable with `GUNICORN_WORKERS` / `GUNICORN_THREADS`.
- **Open:** the CSV exports of shipments and documents still take about 4 s at 3,000 shipments (building about 50,000 Django objects; a rewrite on plain queries would help).
- **Not a defect on inspection:** QA-019 (different bytes, not duplicates), QA-037 and QA-052 (messages exist and render), QA-045, QA-070, QA-087 (synthetic data), QA-023 (`docker-compose.prod.yml` and the droplet script set `DJANGO_DEBUG=0`), QA-016 (`/try/` and `/signup/` are off unless `DEMO_MODE` / `SIGNUP_ENABLED` is set).
- **Left open, product decisions:** QA-022 (which total approval limits use), 040 (per-company shipment numbers), 059 (dark mode, forced colours, rem font sizes).

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
