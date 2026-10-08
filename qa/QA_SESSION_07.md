# ShipMatch QA — Session 07: closing the partial-coverage gaps (6 Oct 2026)

Same rules: real-user testing (browser plus HTTP clients for file uploads and parallel requests), **issues listed, nothing fixed**. App on `http://localhost:8001`. Email stays on the console backend (nothing leaves the machine).
Starting DB backup: `%TEMP%\db.sqlite3.bak-session07-start`. Issue ids continue from session 06 (last was QA-064).

## 1. Coverage closed this session

| Area | Status | Result |
|---|---|---|
| **Disputes: create / edit / validate** | ✅ | Empty/invalid vendor email, bad cc, empty subject or body, bad follow-up date (past, garbage), header-injection attempt in the subject: all refused or neutralised (newline flattened). Gaps: QA-065. |
| **Disputes: send** | ✅ | Email goes to the console with the invoice PDF attached, `Reply-To` = sender, follow-up date flagged. A dispute for a non-money issue rewrites itself to "please send a corrected invoice". Hold on the shipment appears ("can't be approved until the vendor answers"). |
| **Disputes: reply, note, follow-up date, credit, resolve, close, release, discard** | ✅ | Every step has clear validation and messages; release/close need a real explanation (≥10 chars); discard only on drafts. Gaps: QA-066, QA-067. |
| **Disputes: roles** | ✅ | Reviewer can create drafts and is told only approvers/admins send; credit/resolve/close/release → 403. |
| **Rates: create/edit/archive/restore/delete quotes** | ✅ | Archive and restore toggle correctly; delete needs a typed confirmation and is audited; HTML in names is escaped. Gaps: QA-068. |
| **Rates: CSV import** | ✅ | Template imports (2 new); importing it again updates instead of duplicating; "Copy for a new period" and "Check open shipments now" work. |
| **Approved extra charges: create/edit/delete** | ✅ | Strong validation; edit saves but says the wrong thing (QA-069). |
| **Charge names** | ✅ | Built-in names recognised; unknown → "other"; teaching a name applies it and re-checks open shipments; delete works. |
| **Landed cost: spread method, per-category, splits** | ✅ | Splits must add up exactly and be above zero; equal/manual/reset all work; approved/posted shipments refuse changes; other tenants 404. Gap: QA-070. |
| **Month-end: adjustments** | ✅ | Add amount, exclude, replace, remove all change totals correctly. |
| **Month-end: journal Excel content** | ✅ | Journal debits = credits; reversing entry mirrors it exactly; Lines sheet and Notes sheet complete. Gap: QA-071. |
| **Statements: PDF, Excel and CSV** | ✅ | Same file in three formats gives identical lines, balances and findings; non-statement PDFs refused; identical re-upload recognised. |
| **Emailed approval link** | ✅ | Needs sign-in (redirects to login with `next`); tampered/garbage token → friendly 400; expired token still opens (marked expired); another company's token → 404; reviewers get no Approve button. |
| **Notifications** | ✅ | Mentions and assignments create bell items and emails; read-all; open is POST-only; notifications of another user → 404; email preferences switched off stop the emails but keep in-app items. |
| **Vendor learning** | ✅ | A correction creates a profile; forget removes it; repeating forget → 404. |
| **Savings and ROI** | ✅ | ROI inputs range-checked (0–1,000,000, numbers only). Savings swaps an inverted date range silently (extends QA-010). |
| **Keyboard shortcuts** | ✅ | `a` opens a proper confirmation dialog (shows total and open warnings; Escape cancels); `r` focuses the reject reason; `j`/`k`/`x`/Enter move, tick and open; `g q`, `g d`, `g s` navigate; `/` focuses search. |
| **Upload limits and formats** | ✅ | >25 MB per file, >200 files in a ZIP, GIF/BMP refused; TIFF/WebP accepted as scans; 40-page PDF accepted; ZIP bomb refused (QA-072); encrypted PDF fails ugly (QA-002 update). |
| **Concurrency** | ✅ | Two approvers on one shipment: one approval recorded, the other refused by maker-checker. Parallel identical uploads: **HTTP 500** (QA-074). |
| **Webhooks: manage** | ✅ | Validation on create/edit (https only, public addresses, at least one event), signing-secret rotation (old and new valid for 24 h), delete. New endpoints start **off**. |
| **Exports: spreadsheet formula injection** | ✅ | `=`, `@`, `-` prefixes in text are neutralised with a leading apostrophe in the CSV export. |
| Webhook delivery, test, replay | ⬜ | Not run: needs a public receiver (delivery to an outside host). |
| IMAP check/folders, Microsoft/Gmail intake | ⬜ | Not run (outside services). |
| QuickBooks connect/disconnect, Xero | ⬜ | Not run (would break the sandbox connection used earlier). |
| Other browsers, real screen reader, offline/slow network, `DEBUG=False`/PostgreSQL | ⬜ | Not available here. |

## 2. New issues

**QA-065 — Medium — Disputed amount isn't checked against the invoice**
A dispute can be saved with an amount of **0.00** and with **99,999.00 on a 10,615.00 invoice** (the email text is then rewritten for that amount).

**QA-066 — Low — "Credit note" picker offers every document in the organization**
When recording a credit the dropdown lists all 40 documents (freight invoices, bills of lading, other vendors' and other shipments' files), not just credit notes for this vendor/invoice.

**QA-067 — Medium — A credit larger than the dispute is recorded and inflates the "Recovered" KPI**
Recording USD 9,999 against a USD 525 dispute shows a warning but saves; "Recovered this month" then reads **USD 9,999.00**. Require confirmation or cap at the disputed amount (or the invoice).

**QA-068 — Medium — Rate quotes accept questionable data**
- Charge amount **0** accepted although the field hint says "Use a positive amount".
- Two lines with the **same charge** in one quote accepted (ambiguous which applies).
- A **1,000,000,000** rate accepted (no upper bound).
- Identical quotes (same vendor, reference, lane, period) can be created repeatedly with no overlap warning (5 created in a row); the same is true for approved extras.

**QA-069 — Low/Medium — Editing an approved extra says "Nothing changed." although it saved**
Changing free days 4→7 and the daily cap 150→175 stored both values, yet the page said "Nothing changed."

**QA-070 — Low — A shipment's landed-cost method can't be set back to "organization setting"**
The shipment-level select has no empty option (only the per-category selects do), so once someone changes it the shipment is permanently "set for this shipment".

**QA-071 — Low — Journal wording for goods lines**
In the month-end Excel the inventory (supplier goods) lines are described as "Freight accrual (received, not posted)…" and the credit line as "Accrued freight payable". The amounts balance; the descriptions would confuse whoever books it.

**QA-072 — Low — Wrong reason shown for a ZIP bomb**
A 300 MB-of-zeros ZIP (0.3 MB packed) is refused with "no PDF, image or spreadsheet files inside (found big.pdf)". The real reason (compression ratio over 100:1) should be shown.

**QA-073 — Low (verify) — A text-only PDF is classed as a scan**
A 40-page PDF whose pages each contain a short line of real text (drawn by ReportLab) was marked "Scanned image: turn on OCR" and "Needs OCR". Check the threshold used to decide a page has no text layer.

**QA-074 — Medium/High — Parallel identical uploads cause HTTP 500**
Five simultaneous uploads of the same file: one succeeds, **four fail with `IntegrityError: UNIQUE constraint failed: documents_document.organization_id, documents_document.sha256`** (an unhandled duplicate-check race). Only one document is stored, so data is safe, but a double-click on Upload or an email and an upload arriving together shows the user a server error. Catch the constraint and answer "was uploaded before".

### Updates to earlier issues
- **QA-002 (High) widened:** password-protected PDFs, a common real-world case, also show the Python traceback and server file path in the document's error text.
- **QA-003 clarified:** SHP-000041's "container not on the bill of lading" errors **are cleared automatically** when the shared-invoice split is confirmed ("Container X is on the bill of lading of SHP-000045, which carries its share of this invoice. Split confirmed." recorded as an override). The defect is that the page doesn't say so; users see six blocking errors with no hint that confirming the split resolves them. Downgrade to Medium (wording/guidance).
- **QA-010 widened:** Savings also swaps an inverted date range silently.
- **QA-025 clarified:** the `a` shortcut does show a proper confirmation; only the on-page Approve button approves in one click.
- **QA-021 closed** (explained in session 02).

## 3. Worked well
- The dispute flow is complete and careful: every action validates and explains, risky actions need a written reason, the shipment is held while a vendor answers, and roles are enforced.
- Rates import is idempotent; charge names are easy to teach and re-check shipments automatically.
- Landed-cost splits cannot be saved unbalanced; month-end journal is balanced with an exact reversal.
- Emailed approval links are safe (authentication, tamper-proof, tenant-checked, harmless to open).
- File limits are enforced with specific messages; spreadsheet exports guard against formula injection.

## 4. Data state after this session (Acme demo unless noted)
- Disputes DSP-000001 (resolved, USD 9,999 recorded), DSP-000002 (closed), two drafts discarded; SHP-000041's dispute hold released.
- SHP-000044 **approved** (by admin, during the race test).
- SHP-000043 assigned to reviewer with two mention comments and notifications; reviewer's email notifications switched off.
- SHP-000042 landed-cost method changed (weight/quantity), now permanently "set for this shipment".
- Statements 2–4 (Harborlink CSV/PDF/XLSX) uploaded; Apex vendor learning profile forgotten; test quotes, extras, webhooks and charge names created and deleted; month-end adjustments added and removed (no locks created).
- Northwind: extra test documents (multi-page PDF, encrypted PDF, TIFF, WebP, duplicate-race files).
- Restore point: `%TEMP%\db.sqlite3.bak-session07-start`.
