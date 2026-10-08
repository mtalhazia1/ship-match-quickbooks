# ShipMatch QA — Session 05: accounts and admin (4 Oct 2026)

Same rules: real-user testing in the in-app browser, **issues listed, nothing fixed**. App on `http://localhost:8001`.
Account-changing tests were done in the **Northwind Traders (test)** org with its owner account; every credential I changed was put back. Email goes to the console, so reset/invite links were read from the server log.
Starting DB backup: `%TEMP%\db.sqlite3.bak-session05-start`. Issue ids continue from session 04 (last was QA-045).

## 1. Coverage map

| Area | Status | Result |
|---|---|---|
| Password reset (request) | ✅ | Same response for existing and unknown emails (no enumeration); email/case/spaces handled; email text is clear ("works once, for 3 days"). |
| Password reset / set-password page | ✅ | Empty, <12 chars, 11 chars, common, numeric-only, similar-to-email, mismatch all refused with specific text; valid password accepted; reusing the link afterwards says it expired. |
| Password change (signed in) | 🟡 | Wrong current password refused; user stays signed in after a change. **Same password accepted** (QA-046). |
| Sign-in lockout | ✅ | 5 failures lock the username 15 min, **even with the correct password**; unknown and real usernames show the same message; counters are in process memory (a server restart clears them). |
| Open redirect via `next` | ✅ | External/`//`/`javascript:` values land on `/dashboard/`; GET `/account/logout/` → 405. |
| 2FA setup (QR + setup key, confirm) | ✅ | Wrong/blank/short/letters refused; right code turns it on; 10 recovery codes shown once. |
| 2FA sign-in | ✅ | Password step alone gives no access; wrong code stays on the verify page; **5 wrong codes** send you back to sign-in and lock the account; a code from the next 30 s window is accepted; **replaying a used code is refused**. |
| Recovery codes | ✅ | Work once; reuse refused; regenerate needs the password and invalidates old codes. Entry is strict about format (QA-049). |
| Turn off 2FA | ✅ | Needs the password (wrong password → "still on"). |
| Org policy "Require two-factor" | ✅ | A member without 2FA is redirected to the security page for every other URL (pages and API); setting up 2FA lifts it; owner can reset a member's 2FA and the member is forced to set it up again. |
| Team: invite validation | ✅ | Bad role, negative/non-numeric limit, existing member refused. |
| Team: invite accepted via emailed link | ✅ | Strong-password rules apply; sign-in by email works; reviewer role restrictions correct (403 on Team/Settings/Audit/Rates-new). |
| Team: role/limit update, remove, reset 2FA, password link | ✅ | Validation good; **cannot demote or remove yourself**; other tenants' membership ids → 404. |
| **Team: inviting an email that already has an account elsewhere** | ❌ | **Serious — see QA-048.** |
| Org settings (name, currency, zone, MFA, threshold, FX) | ✅ | Bad currency, zone, threshold and FX values rejected; empty name rejected; long name accepted (QA-051). |
| Personal preferences (time zone) | ✅ | Bogus zone ignored; valid/automatic work. |
| Assignment settings & vendor rules | 🟡 | Rejects empty vendor/assignee and an assignee from another company. Round-robin run not exercised. |
| Customs settings | ✅ | Alert days 0–30 and holiday dates validated with named bad values. |
| Client portfolio (`/clients/`), create client | ✅ | Portfolio shows "my work across clients"; **Create client is refused (403, "isn't turned on for your account")** for a normal admin. |
| Platform admin (`/admin/`) | ✅ | Non-superuser → redirected to login; no "Platform admin" link. |
| Vendor learning settings | ⬜ | Not run this session (read in session 01). |
| Notifications bell / email preferences | ⬜ | Not run. |
| Security log / device/session list, "sign out everywhere" | ⬜ | Not found in the UI. |

## 2. New issues

**QA-048 — High (security) — An org admin can add any existing user to their company by email, then reset that user's password**
- Repro: in Northwind (owner), **Team → Invite someone** with `approver@example.com`, an existing account that belongs to another company (Acme).
- Result: no invitation or consent step. The page said "Added approver@example.com to Northwind Traders (test). They can sign in with their existing password." The user appeared in the Northwind team list with her name and **last sign-in time from her other company**.
- It also reveals which emails have accounts anywhere (three different replies: invited / already a member / added existing account).
- Because membership is per company but the user account is shared, the new "admin" can use the row's actions on that account: **Send password link** (the page then shows the one-time set-password link on screen and the code confirms it is built from the user's account), and **Reset two-factor** (`views_team.py` `reset_member_mfa` disables MFA on the shared user). Together that is a path to take over any user whose email is known, including admins of other companies. I generated the password link once to confirm and did **not** open it; her password and Acme login still work with the original credentials.
- Suggested direction: require the existing user to accept the invitation, don't reveal that the account exists, and limit password-link/2FA-reset to users who belong only to the acting organization.
- Cleanup done: I removed her from Northwind.

**QA-046 — Low — Password can be changed to the current password**
`/account/password/` accepts "new = old" and says "Password changed."

**QA-049 — Low — Recovery codes are rendered as adjacent `<span>`s with no separator; entry is strict**
When text is selected or read by a screen reader the ten codes run together. The sign-in box also refused a code typed without the dash and in upper case.

**QA-050 — Low (verify) — Anyone can lock anyone out**
Five wrong passwords for a known username lock that user for 15 minutes (even for the right password). That's a deliberate trade-off, but there's no admin "unlock" and counters are per process, so with several workers the limit multiplies and a restart clears it.

**QA-051 — Low — No server-side length limit on the organization name**
The field has `maxlength=200` in the browser, but a 300-character name was saved. On PostgreSQL a 200-character column would raise an error instead (500). Same worth checking for other free-text fields.

**QA-052 — Low — A few settings forms give no visible result in a scripted submit**
Saving org settings and assignment rules returned the page without a message I could capture. Rejected values were not saved (verified by reading back), so this may just be the success banner; worth a quick eyeball in a normal browser.

**Update to QA-020 (session 01):** the Acme `admin` user is a **superuser and staff** in the seeded database (`reviewer` and `approver` are not), which is why `admin` sees "Platform admin" and `/admin/`. A normal org admin (Northwind owner) does not. The README says that only happens when seeded with `--superuser`, so the question is just whether the demo seed should do that.

## 3. Worked well
- Reset and sign-in never reveal whether an account exists; lockout messages are identical for real and fake users.
- 2FA is solid: replay protection, attempt limit, single-use recovery codes, password to regenerate/disable, org-wide requirement enforced on every route including the API.
- Password rules are strict (12+, not common, not numeric, not similar to the email), shown with specific messages, and enforced on reset, invite and change.
- Team page protects the last admin (can't change or remove yourself) and other tenants' ids return 404.
- Client creation and platform admin are properly gated.

## 4. State left behind
- Northwind: owner password **unchanged** (reset to the same value), 2FA **off**, "Require two-factor" **off**, org name restored. Members `qa-reviewer@example.com` and `qa-approver@example.com` were invited, signed in, then **removed from the team**, but their **user accounts still exist** (no company). Maria's Northwind membership removed; her Acme account untouched.
- Acme data unchanged. Server restarted twice to clear sign-in locks (memory only).

## 5. Next steps
1. Re-test QA-048 once a fix exists (invite an existing email; confirm consent step, no reveal, no password link / 2FA reset on shared accounts).
2. Session 06 — cross-cutting: all pages at 375/768 px, keyboard-only, screen-reader labels, Firefox/Safari, large data volumes, slow/failed network.
3. Remaining small items: vendor learning page actions, notification bell and email preferences, a visible check of QA-052.
