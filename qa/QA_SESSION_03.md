# ShipMatch QA — Session 03: disputes and month-end (4 Oct 2026)

Same rules: real-user testing in the in-app browser, **issues listed, nothing fixed**. App on `http://localhost:8001`, signed in as `admin`. Email uses the console backend (`EMAIL_HOST` empty), so nothing can leave the machine.
Starting DB backup: `%TEMP%\db.sqlite3.bak-session03-start`. Issue ids continue from session 02 (last was QA-031).

## 1. Coverage map

| Area | Status | Result |
|---|---|---|
| **Disputes** — create from a shipment (validation: no issue ticked) | ✅ | Refused with "Choose at least one issue…". |
| Disputes — create draft, draft page loads | ✅ | DSP-000001 created for Harborlink, USD 525.00, subject pre-filled. |
| Disputes — edit, send, reply, note, follow-up date, record credit, resolve, close, release, discard | ⬜ | **Not tested.** The permission classifier blocked my attempt to read the draft's email form (it treats the email-to-vendor flow as a real-world transaction), so I stopped there. Needs your decision (see §5). |
| Disputes — list page, tabs, empty states | ✅ | Draft shows under Drafts 1 / All 1; counts consistent. |
| Month-end — accrual report arithmetic | ✅ | Lines reconcile; changed correctly after the session 02 postings (42 → 36 "received, not booked" = the 6 posted bills). |
| Month-end — period picker / "Other date" parsing | ✅ | Garbage periods silently fall back to the latest month-end (QA-033). |
| Month-end — adjustments: validation | ✅ | No note, `abc`, 0, −50, bad group, bad action, bad shipment (404) all refused with clear text. |
| Month-end — adjustment applied | ✅ | SHP-000039 destination 545.00 → 3,000.00 changed "Not yet invoiced" by exactly +2,455.00. |
| Month-end — lock / versions / exports | ✅ | v1 stays untouched when v2 is locked; stale `expected_version` refused ("locked while you were looking at it"); CSV v1/v2/live and XLSX download with correct totals and file names. |
| Month-end — remove adjustment, "Needs a look" lines, reviewer view | ⬜ | Not run (reviewer 403 was covered in session 01). |
| Month-end settings — validation | ✅ | Required/whole-number/range rules enforced. |
| Vendor statements — read existing statement, findings maths | ✅ | 125.00 + 485.00 + 120.00 = 730.00; "explained / not explained" ties out. |
| Statements — upload edge cases | ✅ | No file, empty, binary-as-CSV, HTML, header-only, junk rows all handled with clear messages. |
| Statements — upload a valid CSV | ✅ | Vendor auto-detected from the file; 2 lines matched; findings and CSV export correct. |
| Statements — PDF / XLSX statements | ⬜ | Not tested. |
| Statements — edit details (vendor, date, currency, balance) | ✅ | All four validated. |
| Statements — mark resolved / reopen | ✅ | Note required; resolve and reopen both work and update counts. |
| Statements — match again, delete | ✅ | Work. |
| Payments — manual record validation | 🟡 | Good except currency (QA-035). |
| Payments — read from QuickBooks | ✅ | "10 new, 0 updated" (sandbox read). |
| "Request a copy" button on a finding | ⬜ | Not clicked (it drafts an email to the vendor). |

## 2. New issues

**QA-032 — Medium — A period can be locked on its own last day, before it has ended**
Locking `2026-10-04` while today is 4 Oct 2026 succeeded ("Locked version 1 for 4 Oct 2026"). The refusal message says "Lock a period after its last day", and a future date is correctly refused, so today looks like an off-by-one (`period > today` instead of `>=`). Locks are permanent, so this matters.

**QA-033 — Medium — A malformed or empty `period` silently locks the latest month-end**
Posting `period=not-a-date` to the lock action did not fail: it fell back to 30 Sep 2026 and **locked version 1 of that month**. Viewing pages with bad periods also quietly show Sep 2026 (same pattern as QA-010). A garbled request shouldn't create an irreversible audit snapshot; return an error instead.

**QA-034 — Low — "Lock version 2" is offered when "Nothing has changed since it was locked"**
The page says nothing changed, yet still offers a new locked version. Probably should be disabled or explained, otherwise audit trails fill with identical versions.

**QA-035 — Medium — Payment currency is silently truncated**
Recording a payment with currency `DOLLARS` succeeded as "DOL 10.00" (junk currency, no error). The statement edit form and rates form reject the same input ("three-letter code"). I deleted the test payment.

**QA-036 — Low — Statement rows that can't be read are dropped without a warning**
A 3-row CSV (one row started with `=HYPERLINK(…)` and contained commas) reported "2 lines matched" and the statement balance (251.00) left out the third row. Nothing said a row was skipped. (This also meant I couldn't confirm CSV-formula escaping in the export; worth retesting with a row that does parse.)

**QA-037 — Low (verify) — No confirmation message after some actions**
After deleting a statement and after "Mark resolved" the page showed no success message (only the state change). Other forms (payments, adjustments, lock) do show one.

**QA-038 — Low — Inconsistent error text on month-end settings**
Look-back days `-5` → "greater than or equal to 0", but `0` → "Use a window between 7 and 1,095 days". Same for "past invoices" (−1 vs 0). Show the real range both times.

**QA-039 — Low (verify) — Dispute draft "To" is empty**
The vendor's address isn't pre-filled, although ShipMatch has received mail from `billing@harborlink.example`. Check whether vendors have a stored billing address anywhere; if not, consider suggesting the sender of the invoice email.

**Update to QA-009 (session 01):** future periods *can't* be locked (server refuses with "hasn't ended yet"), but the "Lock version N" button is still shown for them. Downgrade to a UI nicety.

## 3. Worked well
- Accrual maths reconcile at every level (lines → vendor → journal entry; debits = credit).
- Version history is immutable and clearly labelled; stale-lock protection is good.
- Every adjustment/payment/settings form gives specific, human errors.
- Statements: vendor auto-detection, line matching, findings categories and "Explained / Not explained" arithmetic.
- Exports have sensible file names (`accruals-2026-09-30-v2.csv`, `statement-swift-cargo-forwarding-2026-09-30.csv`).

## 4. Data state after this session (Acme demo)
- **Locked and permanent:** Sep 2026 **v1** (accidental, from QA-033) and **v2** (includes my adjustment); **4 Oct 2026 v1** (from QA-032). The dashboard now reads Month-end Sep 2026 "Locked".
- Adjustment kept: SHP-000039, destination port and customs → USD 3,000.00 (note "QA session 03…").
- Dispute **DSP-000001** (draft, unsent) for Harborlink / invoice HL-880075 / USD 525.00.
- 10 payments imported from the QuickBooks sandbox (read-only call).
- Created and removed again: one junk payment ("DOL 10.00"), one test statement (Swift Cargo CSV). Statement 1 findings left open as found.
- Restore point: `%TEMP%\db.sqlite3.bak-session03-start` (returns to the state after session 02 posting).

## 5. Decisions needed / next steps
1. **Disputes send flow:** to finish disputes (edit → send → reply → credit → resolve/close → release) I'd fill the vendor address and send. Email is console-only, so nothing leaves the machine, but the classifier blocked it. Tell me if you want me to proceed (or run the dispute lifecycle yourself and I'll review).
2. **Locks:** keep them, or restore `db.sqlite3.bak-session03-start` to drop the accidental Sep 2026 and 4 Oct locks (this also removes the draft dispute and QuickBooks payment import).
3. Next session candidates: finish disputes; statement PDF/XLSX; remove-adjustment; "Needs a look" lines; journal XLSX content (accounts/vendor mapping, reversal date).
