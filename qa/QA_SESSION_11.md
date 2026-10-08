# ShipMatch QA — Session 11: export safety, password reset, caching after sign-out (8 Oct 2026)

Same rules: real-user testing (browser plus the Django test client for downloads and emails), **issues listed, nothing fixed**. App on `http://localhost:8001`. Email was captured in memory, nothing left the machine.
Starting DB backup: `%TEMP%\db.sqlite3.bak-session11-start`; **restored at the end**. Issue ids continue from session 10 (last was QA-081). Branch under test: `fix/qa-session-08` (includes the session 10 fixes). The full test suite on this branch: 1,065 passed, 1 skipped.

## 1. Coverage this session

| Area | Status | Result |
|---|---|---|
| **Spreadsheet formula injection (a vendor name and invoice number set to `=HYPERLINK("http://evil.example","click")`)** | ✅ | Month-end accruals CSV, audit CSV, landed-cost report CSV, per-shipment landed CSV, the three API exports (shipments, documents, issues) and the three Excel files (month-end journal, landed report, per-shipment landed) were all downloaded. No cell is a live formula: CSV and API cells that start with a formula character are prefixed with `'`; in the journal workbook the text sits in a string cell. The original values were put back afterwards. |
| **Password reset** | ✅ | Known and unknown addresses get the identical page and message ("If an account uses that address…"); only the known one sends an email. Capital letters in the address work. Malformed and empty addresses re-show the form. The email names the account and says the link works once, for 3 days. Timing: after a cold first request, known and unknown addresses answered in about the same time (18 ms and 12 ms). |
| **Caching of signed-in pages and downloads** | ✅ | Gap: QA-082. |
| **Back button after signing out** | ✅ | In the in-app browser, Back to a signed-in shipment page made a new request and landed on the sign-in page ("next" kept), so nothing was shown from cache. Not checked in other browsers (QA-082). |

## 2. New issues

**QA-082 — Low (verify) — Signed-in pages and downloads carry no cache directive**
The sign-in page sends `no-store`, but the dashboard, queue, shipment pages, audit log, settings, disputes, month-end and every CSV/Excel/API download send no `Cache-Control` at all, and the original-file view sends `private, max-age=300`. They do vary on the cookie. On a shared computer a browser that restores pages from memory after sign-out could show the last signed-in page, and a proxy or browser may keep a copy of an export. Sending `Cache-Control: no-store` (or `private, no-store`) for signed-in responses, and for the file view too if that is acceptable, removes the question. The in-app browser did not show the problem.

## 3. Notes (not issues)

- In the Excel journal the neutralised text appears with a leading `'` that is part of the cell value (it is not Excel's hidden quote prefix), so an accountant would see the apostrophe. Only matters for names that start with `=`, `+`, `-` or `@`.
- The reset email's sender is `no-reply@example.com` and its link uses the request's host; both come from configuration (`DEFAULT_FROM_EMAIL`, `SITE_URL`) and should be set in production.

## 4. Still untested

Real webhook delivery and replay (needs a public receiver); real IMAP / Microsoft / Gmail mailboxes; QuickBooks disconnect / reconnect and Xero connect; **PostgreSQL** (Docker 29.8.1 is installed here; running Postgres needs the `postgres` image, a download of roughly 150 MB that I did not start without asking); other browsers and a screen reader; posting to QuickBooks twice at once.

## 5. Test data left behind

None: the database was restored from the starting backup.
