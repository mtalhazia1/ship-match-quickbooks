# ShipMatch QA — Session 08: webhooks, accounting settings, DEBUG=False (7 Oct 2026)

Same rules: real-user testing in the in-app browser plus HTTP checks, **issues listed, nothing fixed**. App on `http://localhost:8001` (normal) and `http://localhost:8002` (`DJANGO_DEBUG=0`, no TLS redirect). Email stays on the console backend.
Starting DB backup: `%TEMP%\db.sqlite3.bak-session08-start`. Issue ids continue from session 07 (last was QA-074). The branch under test includes the fixes from `fix/qa-remaining`.

## 1. Coverage this session

| Area | Status | Result |
|---|---|---|
| **Webhooks: add endpoint, address validation** | ✅ | Refused with a clear reason: `http://`, `localhost`, `127.0.0.1`, `10.x`, `169.254.169.254` (cloud metadata), `[::1]`, port 444, user:password in the address, `ftp://`, text with spaces, unresolvable hosts. Gap: QA-076 (long host echoed). |
| **Webhooks: edit, remove** | ✅ | Edit applies the same checks; "Choose at least one event" enforced; remove says "Nothing more is sent to it". |
| **Webhooks: real delivery, test event, retries, replay** | 🟡 | Not run: endpoints must be public https, and sending to an outside service would publish data. Covered by the automated tests only. Needs a public receiver the team controls. |
| **QuickBooks settings page (read)** | ✅ | Connected sandbox company, valid until 12 Jan 2027, vendor account rules listed. Disconnect and "Connect again" not exercised (would break the sandbox connection used in session 02). |
| **QuickBooks default expense account** | ✅ | Gap: QA-075. |
| **Xero** | 🟡 | Page renders "Not connected / not set up on this server" with setup steps. Connect flow needs a Xero app. |
| **Behaviour with DEBUG off** | 🟡 | 404 page has no debug detail; `/health/` 200; security headers present (CSP, nosniff, same-origin referrer, frame options); CSRF cookie is `Secure`; unknown `Host` → 400; `/admin/` and `/api/openapi.json` need sign-in (302). Static files could not be checked: `staticfiles/` was not built (`collectstatic`). Gap: QA-077. |

## 2. New issues

**QA-075 — Medium — QuickBooks "default expense account" accepts any value and says it was saved**
Posting `999999`, `abc`, `<script>` and an empty value all returned "Default expense account saved." None of them is an account in the list; the page then shows the select blank. A bogus id would be sent to QuickBooks when a vendor has no rule of its own, so bills fail at posting time. The value should be checked against the account list the page offers (or cleared deliberately). Original value (69, Accounting) was restored.

**QA-076 — Low — A long host name is echoed in full in the webhook error**
A 300-character host name produced an error message repeating all 300 characters. (Correction made when fixing: the address itself is already capped at 2,000 characters by the form check, so the accepted 620-character address was within the limit and is not a defect.)

**QA-077 — Low (verify) — API reference page loads Swagger UI from jsdelivr and an icon from django-ninja.dev**
`/api/docs` pulls `swagger-ui.css` and `swagger-ui-bundle.js` from `cdn.jsdelivr.net` and favicons from `django-ninja.dev`. It will not render without internet access (offline or locked-down networks) and depends on third-party hosts. It also carries no Content-Security-Policy, unlike the app's pages, which would block these scripts. Decide whether to vendor the files.

## 3. Notes (not issues)

- The QuickBooks and Xero redirect URIs on the settings page still show `:8000` while this server runs on `:8001` (known: QA-031, comes from `.env`).
- "Payment status: last checked 3 days ago" is expected without the hourly scheduler running locally.
- The QuickBooks sandbox chart of accounts lists "Equipment Rental (Expense)" twice (sandbox data).

## 4. Still untested

Real webhook delivery and replay; real IMAP / Microsoft / Gmail mailboxes; QuickBooks disconnect / reconnect and Xero connect; PostgreSQL; `collectstatic` output with DEBUG off (cache headers, compression: QA-063); other browsers and a screen reader.

## 5. Test data left behind

None. The test webhook endpoint (id 3) was removed; the QuickBooks default account was restored to 69.
