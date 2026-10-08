# ShipMatch QA — Session 10: concurrent requests (7 Oct 2026)

Same rules: real-user testing in the in-app browser (parallel `fetch` calls from a signed-in page, as a double-click or two people would produce), **issues listed, nothing fixed**. App on `http://localhost:8001`, SQLite (the documented local-run database). Email stays on the console backend.
Starting DB backup: `%TEMP%\db.sqlite3.bak-session10-start`; **restored at the end**, so the demo data is back to its starting state. Issue ids continue from session 09 (last was QA-078). Branch under test: `fix/qa-session-08`.

## 1. Coverage this session

| Area | Status | Result |
|---|---|---|
| **Confirm a shared invoice's split, 6 at once** | ✅ | One request succeeded; five returned **HTTP 500, "database is locked"**. The split ended confirmed once, with no duplicate state. Gaps: QA-079. |
| **Approve the same shipment twice at once** | ✅ | Both requests succeeded: two approval records and two `shipment.approved` audit events. A second approve sent *after* the first finished is correctly refused ("This shipment is already approved"). Gaps: QA-080. |
| **Approve and reject the same shipment at once** | ✅ | Both succeeded: a reject (with its note) and an approve were recorded; the shipment ended **approved**. A reject sent after the approval is correctly refused. Gaps: QA-081. |
| **Start a dispute on the same invoice, 3 at once** | 🟡 | One draft created; two requests returned 500 "database is locked" (QA-079). Whether two drafts could be created on a database that does not lock (PostgreSQL) was not tested; the "already open" check is a read followed by a create, so the same window exists. |
| **Duplicate outgoing events** | 🟡 | The demo organization has no webhook endpoints, so duplicate delivery from QA-080 could not be observed. |
| **Posting to QuickBooks twice at once** | ⬜ | Not run: it would create bills in the sandbox. The posting code uses a request id for idempotency (session 02), so it is the likeliest to be safe. |

## 2. New issues

**QA-079 — Medium (Low in production) — Concurrent writes on SQLite return HTTP 500 "database is locked"**
Six simultaneous POSTs to one action gave one success and five 500s; the same happened with simultaneous dispute creation (two of three) and the approve/reject pairs partly. The 500s come from SQLite refusing to upgrade a read transaction to a write while another writer holds the lock, instead of waiting. Anyone running the local SQLite setup with two people or a double-click sees "Something went wrong". Django can start write transactions in `IMMEDIATE` mode and wait longer (`OPTIONS: {"transaction_mode": "IMMEDIATE", "timeout": 20}`), so requests queue instead of failing. Production on Postgres is not affected, but it also means this lock was masking the races below during this test.

**QA-080 — Medium — Approving the same shipment twice at once records it twice**
`review.approve` checks "already approved" and then writes, with no transaction or row lock between the two. Two simultaneous approvals (a double-click, or two approvers) each pass the check: two `Approval` rows and two `shipment.approved` audit events, which later also means two notifications and two webhook events. Fix: lock the shipment row inside a transaction (`select_for_update`) and re-check the status after locking.

**QA-081 — Medium — Approve and reject sent at the same moment are both recorded; the shipment ends approved**
With one approver approving and the same account (or a second approver) rejecting at once, the history shows a rejection with its note and an approval, and the status is "Approved". The rejection reason is in the log but did not stop the approval. Same cause and fix as QA-080 for `review.reject` and the other state changes (reopen, post).

## 2b. Fixed afterwards (same day)

- **QA-079:** on SQLite, `config/settings.py` now starts write transactions in `IMMEDIATE` mode with a 20-second wait, so simultaneous requests queue.
- **QA-080 / QA-081:** `approve`, `reject` and `reopen` in `apps/shipments/views.py` now lock the shipment row inside a transaction and run their checks after locking (`select_for_update`; on SQLite the `IMMEDIATE` mode gives the same one-at-a-time effect).
- **Re-run in the browser, same races:** six simultaneous split confirmations, all redirected, zero "database is locked" in the log (before: five 500s); three simultaneous approvals of SHP-000042 gave **one** approval and one audit event, the others "already approved"; approve against reject on SHP-000043 gave one approval and "already approved. Reopen it first." for the reject; three simultaneous dispute requests gave one draft. The database was restored afterwards.
- Tests: `tests/test_qa_remaining.py` simulates the race by giving the view a stale copy of the shipment; the two race tests fail without the lock and pass with it.

## 3. Notes (not issues)

- Sequential repeats are handled well: a second approve, or a reject after approval, is refused with a clear message.
- Confirming a split five times at once left the split confirmed once, with its issues resolved once.

## 4. Still untested

Real webhook delivery and replay (needs a public receiver); real IMAP / Microsoft / Gmail mailboxes; QuickBooks disconnect / reconnect and Xero connect; PostgreSQL (including whether QA-080/081 and the dispute race show up there); other browsers and a screen reader; posting to QuickBooks twice at once.

## 5. Test data left behind

None: the database was restored from the starting backup.
