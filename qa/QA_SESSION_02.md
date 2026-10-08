# ShipMatch QA — Session 02: core approval loop (4 Oct 2026)

Same rules as session 01: real-user testing in the in-app browser, **issues listed, nothing fixed**.
App run on `http://localhost:8001` (port 8000 is serving a different project). Accounts: `admin`, `approver` (Maria Lopez, limit USD 50,000), `reviewer` (Sam Patel).
Starting DB backup: `%TEMP%\db.sqlite3.bak-session02-start` (restore this to reset the demo data).
Issue ids continue from session 01 (QA-001 … QA-023). Session 01 also gained: **QA-024… below**.

## 1. Coverage map for this session

| Step in the loop | Status | Result |
|---|---|---|
| Pick a candidate shipment from the queue | ✅ | Queue tabs/counts consistent with dashboard (see QA-028 re: counts). |
| Accept a warning (reviewer/admin) | ✅ | Works; shipment moved to "Ready to approve", queue count 18 → 17. |
| Confirm a shared-invoice split | ✅ | Works; also auto-accepted the sibling shipment's warning (SHP-000046) in Maria's name. |
| Fix a value, then re-validate | 🟡 | Covered in session 01 (QA-001/005). Not repeated with valid values. |
| Override an **error** (approver) | ✅ | Reviewer → 403; empty/whitespace note refused; ≥10-char note accepted; recorded in audit log. |
| Approve (UI button + direct POST) | ✅ | Works for Maria on SHP-000045, 000021, 000027. Admin approved 000046 via the real button. |
| Maker-checker | ✅ | Admin who accepted a warning can't approve (UI + `/review/…/approve/` + `/work/…/approve/`). Maria who confirmed a split can't approve the sibling she "prepared". |
| Approval limit | ✅ | Total 25,742.64: limit 25,000 blocked; 25,742.63 blocked; 25,742.64 approved (boundary exact). Message names both amounts. |
| Reject (reason required) | ✅ | Empty/whitespace refused; with reason works, logged. |
| Reopen after reject | ✅ | Works; logged. |
| Reopen / reject a **posted** shipment | ✅ | Refused with clear messages. |
| Edit a field on an **approved** shipment | ✅ | Refused server-side ("locked. Reopen it to make changes."). |
| Bulk: assign / unassign / invalid ids / none selected | ✅ | All handled with clear messages. |
| Bulk approve of blocked shipments | ✅ | Shows a dry-run page ("0 will be approved, 4 will be skipped") with a reason per shipment. |
| Dashboard / queue counts after approvals | ✅ | Needs review 18 → 14, Ready 1, Approved-not-posted 2 → 5. |
| Audit log records the actions | ✅ | Accept, override, assign, approve, sign-in/out all present. |
| **Post bills to QuickBooks** | ⬜ | **Not run — needs your OK.** `.env` holds real Intuit *sandbox* keys, so posting creates bills + PDF attachments in your external sandbox company. |
| Payment status check ("Check payments now") | ⬜ | Depends on posting. |
| Emailed approval link `/approve/<token>/`, quick-approve panel | ⬜ | Not reached. |
| Approve while a sent dispute waits for the vendor | ⬜ | Belongs with session 03 (disputes). |
| Foreign-currency approval limit (EUR rate) | 🟡 | SHP-000021 has EUR + USD totals; approved fine with the 1.08 rate. Missing-rate case not run. |
| Two-factor required at approval | ⬜ | Needs 2FA enabled first (session 05). |
| Two approvers racing on one shipment | ⬜ | Not run. |
| Keyboard `a` / `r` shortcuts and the confirm dialog | ⬜ | Only the button was tried (see QA-025). |

## 2. New issues

**QA-024 — Medium — Shipment is labelled "Ready to approve" but cannot be approved**
After accepting the shared-invoice warning on SHP-000045 the status badge and queue tab say "Ready to approve", yet the banner still says "You can't approve this yet — Confirm the split of shared invoice HL-880080 first" and Approve is disabled. Accepting the warning does not confirm the split (card still "Not confirmed"). Two actions that look the same, one label that overpromises.

**QA-025 — Medium (verify) — Approve button approves in one click, no confirmation**
Clicking **Approve** on SHP-000046 approved it immediately. The shortcuts help says for `a`: "A confirmation opens first; nothing is approved until you confirm." (Reopen exists, but approval changes status, locks the shipment and notifies.) Decide whether the button should confirm like the shortcut, and test the `a` dialog.

**QA-026 — Low — Audit log "Record" column shows `SimpleLazyObject 3`**
Sign-in/sign-out rows show the record as "SimpleLazyObject 1/2/3" instead of the user (e.g. "Account"). Visible on `/audit/` (and likely in the CSV).

**QA-027 — Low (verify) — Denied actions are not audited**
Sam (Reviewer) trying to override an error got a 403 but nothing appears in the audit log. Decide if blocked attempts should be recorded.

**QA-028 — Low — Mixed-currency totals run together**
Bulk-approve preview shows `USD 2,880.18EUR 11,063.00` for SHP-000021 (no separator); on the shipment header the same shipment reads "USD 2,880.18," with a trailing comma. Check how a mixed-currency payable total should be displayed.

**QA-029 — Low (verify) — Confirming a split quietly accepts siblings' warnings under your name**
On SHP-000045 Maria confirmed the split; SHP-000046's warning was auto-accepted "by Maria Lopez", which then made her the preparer of 046 and blocked her from approving it. Consistent with maker-checker, but surprising — consider telling the user which sibling shipments are affected.

## 3. Worked well this session
- Every gate is enforced server-side as well as in the UI (maker-checker, limit, locked shipment, posted shipment, role).
- Messages are specific: "Shipment total USD 25,742.64 is above your approval limit of USD 25,000.00."
- Reject/override need a real reason; override needs an Approver.
- Bulk approve is a two-step preview, not a blind action.
- Activity timeline on each shipment reads clearly (who, what, when, reason).

## 4. Data state changes (for the next session)
- Approved this session: **SHP-000021, 000027, 000045, 000046** (000028 and 000039 were already approved; 8 shipments already posted).
- SHP-000021: error "Harbor maintenance fee is wrong" overridden by admin (note "…QA session 02").
- SHP-000027: both warnings accepted by admin.
- HL-880080 split confirmed (by Maria). SHP-000046 was rejected then reopened (history stays in activity).
- Maria's approval limit was changed and **restored to 50,000.00**. Sam's assignment toggled on 000042/000043 and cleared.
- Nothing was posted to QuickBooks. Nothing else outside Acme was touched.

## 4b. Posting to the QuickBooks sandbox (done 4 Oct 2026, after approval from the user)

Connected company: *Sandbox Company US dd88* (token valid to 12 Jan 2027). Backup before posting: `%TEMP%\db.sqlite3.bak-before-posting`. Bills now exist in that external sandbox.

| Shipment | Result |
|---|---|
| SHP-000021 | **Partial.** Bills `HA-556641` and `CCB/24117` posted. The EUR commercial invoice failed: "The invoice is in EUR, but multicurrency is off in QuickBooks (home currency USD). Turn on multicurrency in QuickBooks, or enter the bill there by hand." Shipment stays **Approved**. |
| SHP-000027 | Posted: `CI-17492`, `SW-808011`. Status "Posted to accounting". |
| SHP-000045 | Posted: `CI-60801`. Shared freight invoice HL-880080 correctly **not** posted (waits for SHP-000044). |
| SHP-000046 | Posted: `CI-60802`. Same shared-invoice wait. |

Also verified
- ✅ **Idempotent:** posting SHP-000021 a second time created no duplicate bills (same two bill numbers, same two "posted" activity entries; the EUR one failed again with the same message).
- ✅ Bill numbers deep-link to the sandbox (`…/app/bill?txnId=166`).
- ✅ **Check payments now:** "Checked 25 bills in QuickBooks. 23 changed status." Dashboard then read 11 posted, 0 "not checked yet", Approved-not-posted 3 (21, 28, 39).
- ✅ This explains QA-021: the earlier "17 posted bills not checked yet" were just never-polled bills. Unpaid total moved from USD 29,462 to USD 373,151 once they were read.
- ⬜ Not verified: that the PDF is actually attached to each bill inside QuickBooks (needs an Intuit login); the failed-token / disconnected path; the Xero path (not configured).

New notes
- **QA-030 — Low (verify):** a shipment that is only partly posted stays "Approved" with no banner saying "2 of 3 bills posted, 1 failed"; the failure is only visible inside the document card further down. Consider a header-level summary.
- **QA-031 — Info:** QuickBooks settings shows Redirect URI `http://localhost:8000/…` while the app was reached on :8001 (value comes from `.env`). Fine for the sandbox, but anyone running on another port would hit an OAuth redirect mismatch when reconnecting.

## 5. Open question / next steps
1. Posting is now tested (section 4b). Still to do for the loop: approve SHP-000044 (needs its 3 errors dealt with) to see the shared invoice HL-880080 post once for 044/045/046; failed-token path (disconnect/reconnect); PDF attachment check inside QuickBooks.
2. Run the remaining ⬜ rows above (approval link, `a`/`r` shortcuts, 2FA gate, race, missing FX rate).
3. Re-check QA-024/025 after any change; both are quick to re-verify.
