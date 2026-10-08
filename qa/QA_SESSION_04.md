# ShipMatch QA — Session 04: intake and integrations (4 Oct 2026)

Same rules: real-user testing in the in-app browser, **issues listed, nothing fixed**. App on `http://localhost:8001`.
All destructive work was done in the empty **Northwind Traders (test)** org, so Acme demo data was not touched. Nothing contacted an outside service (no webhook deliveries, no IMAP connections, no QuickBooks connect/disconnect).
Starting DB backup: `%TEMP%\db.sqlite3.bak-session04-start`. Issue ids continue from session 03 (last was QA-039).

## 1. Coverage map

| Area | Status | Result |
|---|---|---|
| Upload a normal PDF | ✅ | Becomes a shipment; a lone commercial invoice gets "Bill of lading not received" warning. |
| Duplicate handling | ✅ | Same file again, and same bytes under a new name, are both refused with "…was uploaded before… It is in SHP-0000nn". |
| ZIP of related documents | ✅ | 3 documents unpacked and grouped into one shipment (SHP-000070, "Ready to approve", no issues). |
| ZIP edge cases | ✅ | Path-traversal entry refused ("unsafe file path inside the ZIP"); ZIP with only `.txt` files refused with the list found. Nested ZIP not unpacked (QA-045). |
| Photos / scans (PNG, JPG) | ✅ | Received, marked "Needs OCR" (OCR is off here), message explains how to continue. |
| Spreadsheet / CSV uploaded as a document | ✅ | Received as type "Other", "No shipment found". |
| Manual rescue of an unreadable scan | ✅ | Typing a B/L (`" oslN2181241943 "`) normalised it and moved the scan into SHP-000070. |
| Multi-file upload (PDF + image) | ✅ | Works; duplicate in the batch is reported separately. |
| Invalid uploads (empty, renamed exe, html, corrupt zip, traversal filename) | ✅ | Covered in session 01; still clear. |
| Very large files, TIFF/WebP, password-protected or multi-page PDFs, ZIP bombs, upload rate limit | ⬜ | Not tested. |
| Inbound email endpoints (`/inbound/email/postmark/`, `/mailgun/`) | ✅ | Reject without credentials (401 / 403 "Invalid signature"); GET → 405. |
| Forwarding address (generate/regenerate) | ⬜ | Not set up on this server (`INBOUND_EMAIL_DOMAIN` empty), page explains this. |
| Email intake settings (allowed senders, pause) | 🟡 | Page read only. |
| IMAP mailbox form — validation | ✅ | Required fields, port range, security choice, bad host formats, private/local addresses (see QA-041). |
| IMAP mailbox — real connection, folders, check now, toggle, edit | ⬜ | Not tested (would connect to an outside server). |
| Microsoft 365 connect / callback | ✅ | Not configured → clear message and redirect back; bogus callback refused. |
| Gmail label intake | ⬜ | Not configured. |
| QuickBooks connect / reconnect / disconnect | 🟡 | Callback with a bogus state is refused with "link expired or opened twice". Connect/disconnect **deliberately not run** (disconnect tells Intuit to cancel the connection that the session 02 postings used). |
| Xero | 🟡 | Not configured; `/accounting/xero/connect/2/` 404, callback refused. |
| API keys: create / validation / scopes / expiry options / revoke | ✅ | See §3. |
| Public API: auth, scopes, tenant isolation, upload, exports | ✅ | See §3. |
| Webhooks: create, test, replay, rotate, delete | ⬜ | URL validation was covered in session 01. Creating a real endpoint would deliver real events to an outside host. Needs a safe receiver (e.g. a local listener). |

## 2. New issues

**QA-040 — Low (verify) — Shipment numbers are one global sequence across companies**
Northwind's first shipment was **SHP-000069**, and the duplicate message says "It is in SHP-000069". A customer can infer how many shipments other tenants have created. Consider per-organization numbering (or confirm it's acceptable).

**QA-041 — Medium — IMAP form saves the mailbox even when the connection test fails**
Submitting hosts `127.0.0.1`, `169.254.169.254` and `10.0.0.5` was correctly refused for connecting ("an address on a private network, which ShipMatch doesn't connect to") but **each attempt still created a saved mailbox** (with the typed password) and landed on its edit page, so a wrong host leaves a broken, credential-holding row behind. Related inconsistencies: `localhost` and `[::1]` are rejected by the form while `127.0.0.1` is only rejected after saving; `imap://evil.example` is accepted (scheme silently stripped) and triggers a DNS lookup for that host. I removed the 4 mailboxes I created.

**QA-042 — Low — API accepts nonsense filters**
`GET /api/<org>/shipments?status=bogus` → 200 `[]`; `?limit=-1` → 200 with data; `?limit=1000000` → 200. Better to return 422 for unknown status and clamp or reject out-of-range limits.

**QA-043 — Low — API (and web) upload accept files that only *start* like a PDF**
A 10-byte `%PDF-1.4 x` was accepted by the API (201) and, in session 01, by the web upload; it later fails with the traceback text (QA-002). Validate the PDF structure on upload (or at least fail with a friendly message).

**QA-044 — Low — Env-var names in the scan message**
The message after uploading a photo ends "An admin can turn on reading of photos and scans with `EXTRACTION_PROVIDER=anthropic` or `OCR_PROVIDER=textract`." Shown to every uploader (extends QA-013).

**QA-045 — Low — Nested ZIP message is confusing**
Uploading a ZIP that contains another ZIP says "0 documents added. 1 was received before (inner.zip)". The inner ZIP isn't opened, and "received before" is misleading (it matched the earlier upload of the same bytes). Say plainly that nested ZIPs aren't supported.

## 3. Worked well
- **API keys:** empty name and no-scope are refused with clear text; "Read only" vs "Read and upload"; scopes per key; 30/90/365 days or never; the secret is shown **once** ("stored only as a hash") with a Copy button; the table shows only a prefix, creator, created/last-used, status; **revoke is immediate** (next request → 401).
- **API behaviour:** no/bad key → 401; missing scope → 403 *naming the scope and where to get it*; read-only key can't upload (403 with explanation); `/api/<other-org-slug>/…` → 404; another tenant's shipment id → 404; bad path id → 422; upload with a reviewer key → 201; CSV export works.
- Inbound email endpoints are authenticated (Postmark credentials / Mailgun signature).
- OAuth callbacks reject unknown state; not-configured integrations explain what's missing instead of failing.
- Dedupe is by content, not file name.

## 4. State left behind (Northwind test org only)
- Shipments SHP-000069 (lone invoice), SHP-000070 (3-document ZIP + scan.png moved in by hand), SHP-000071 (S05 invoice); documents: `scan.jpg`, `stmt.csv`, `stmt.xlsx`, `a.pdf` (API upload), `passwd.pdf` (session 01).
- 5 API keys created, all **revoked**. 4 test mailboxes created and **removed**.
- The temporary folder `media/qa-tmp/` I used to serve test files was deleted. Acme data unchanged.

## 5. Next steps
1. Webhooks end to end with a local listener (run a tiny HTTP server and allow it, or ask for a `webhook.site`-style receiver): create, signature header, retry on 500, replay, rotate secret, delete, delivery log.
2. Email intake with the inbound webhook using real Postmark-style JSON (needs the shared secret) and allowed-senders filter; IMAP check/folder refresh against a throwaway mailbox you control.
3. Upload limits: large files, TIFF/WebP, password-protected PDF, many-page PDF, upload rate limit, concurrent uploads of the same file.
4. QuickBooks reconnect/disconnect (only once you're happy to break and restore the sandbox connection).
