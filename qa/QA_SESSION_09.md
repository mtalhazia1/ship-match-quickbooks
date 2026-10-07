# ShipMatch QA — Session 09: static files with DEBUG off, fix re-check, input and tenant sweeps (7 Oct 2026)

Same rules: real-user testing in the in-app browser plus HTTP checks, **issues listed, nothing fixed**. App on `http://localhost:8001` (normal) and `http://localhost:8002` (`DJANGO_DEBUG=0`, no TLS redirect, after `collectstatic` into a temporary `staticfiles/` that was deleted afterwards). Email stays on the console backend.
Starting DB backup: `%TEMP%\db.sqlite3.bak-session09-start`. Issue ids continue from session 08 (last was QA-077). Branch under test: `fix/qa-session-08` (includes `fix/qa-remaining`).

## 1. Coverage this session

| Area | Status | Result |
|---|---|---|
| **Static files with DEBUG off (QA-063)** | ✅ | `collectstatic` copied 163 files. CSS, JS, pdf.js and the Swagger bundle are served **gzip-compressed** (`app.css` 90 KB → 18 KB; `swagger-ui-bundle.js` 1.5 MB → 419 KB; fonts are already compressed). ETags present. Gaps: QA-078. |
| **Re-check of earlier fixes in the browser** | ✅ | Time-zone list 314 entries with no deprecated aliases; `main` takes focus from the skip link; muted text colour `#566578`; `page=0` and `page=-3` show the first page; queue title names the tab ("Review queue – Ready to approve"); landed report says "the two were swapped"; savings says "Those dates could not be read"; API: session 200, bogus Bearer 401, `status=bogus` 422, `limit=0` 422, `limit=1000` 200, other tenant 404. |
| **Search and filter boxes, odd input** | ✅ | 120 requests across the global search, review queue, disputes, rates and statements with empty, spaces, quotes, SQL-looking text, `%`, `_`, backslash, `<script>`, 5,000 characters, NUL, emoji, template syntax, path traversal: no 5xx. Reflected text is escaped on all four pages that show the query. |
| **Tenant isolation (Northwind owner against Acme data)** | ✅ | Shipment, document, document file, landed exports, dispute, quote, API shipment/document, QuickBooks settings and webhook ids belonging to another organization all return 404. `?org=2` on audit, queue, notifications and the audit CSV export is ignored: only Northwind's own events and shipments appear (the CSV also holds the members' own sign-in and security events, by design). |

## 2. New issues

**QA-078 — Low — Static files are cached for only 60 seconds and have no content hash in the name**
With `DEBUG` off, WhiteNoise sends `Cache-Control: max-age=60` for every static file, including the 1.5 MB Swagger bundle, the 1.8 MB pdf.js files and the fonts, and the files are named `app.css`, `app.js` and so on. Browsers recheck them each minute (a cheap `304`, but it is a request per file per page), and a deploy cannot be cached for a year safely. Only gzip is produced, not brotli. Using `ManifestStaticFilesStorage` (hashed names) with a long `WHITENOISE_MAX_AGE`, or the proxy's cache rules in `deploy/Caddyfile`, would fix both. This refines QA-063: compression works, caching is the gap.

## 3. Notes (not issues)

- Searched text is only reflected escaped; no raw `<img>`/`<script>` in any response.
- Shipment numbers in a tenant's own pages still come from the global sequence (QA-040): the Northwind shipments carry the high numbers of the shared counter.
- The audit log's filter by organization (`?org=`) is ignored, which is the safe behaviour.

## 4. Still untested

Real webhook delivery and replay (needs a public receiver); real IMAP / Microsoft / Gmail mailboxes; QuickBooks disconnect / reconnect and Xero connect; PostgreSQL; other browsers and a screen reader; concurrent approvals of the same shipment by two people.

## 5. Test data left behind

A few audit rows in the Northwind test org from the tenant checks (`audit.exported` and page views by its owner). No other data changed; the temporary `staticfiles/` folder was removed.
