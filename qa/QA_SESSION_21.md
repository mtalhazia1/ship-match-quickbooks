# ShipMatch QA — Session 21: security deep-dive (16 Oct 2026)

Same rules: **issues listed, nothing fixed**. Same environment as session 18 (throwaway PostgreSQL 16 and Redis containers, app on `127.0.0.1:8002`, `DEBUG` on, console email). Branch under test: `merge/qa-sessions-08-13` at `b94b52f`. Issue ids continue from session 18 (last was QA-096).

## 1. Method

- **Role-by-route matrix.** Every web route (190 after leaving out `/admin/`, `/api/` and `/media/`), GET and POST, as six identities: anonymous, viewer, reviewer, approver, admin of the demo company, and admin of a second company (`qa-other`). Route parameters were filled with real demo objects (one of each kind was created where the demo had none: API key, dispute, payment, adjustment, mailbox, comment, vendor rule, webhook endpoint and delivery, charge name). Django's test client ran in-process, and **every request ran inside a transaction that was rolled back**, so admin POSTs could be tried without changing anything. A request counted as "changed" if any app table's row count, the target object or any membership differed before the rollback. 2,280 requests. (A first run of all six roles at once was stopped by the machine running low on memory; it was re-run one role at a time.)
- **API matrix.** The 6 API operations × 17 identities: sessions of each role, API keys of each access level and scope, revoked, expired, other company's key, garbage key, right prefix with the wrong secret, a session plus a bad key.
- Session handling, CSRF, rate limits, server-side request checks, hostile uploads, secrets in logs, dependency scan.

## 2. Coverage this session

| Area | Status | Result |
|---|---|---|
| **Company isolation, every web route** | ✅ | The other company's admin got 404 (or a refusal) on every route that names a demo object; nothing changed. |
| **Anonymous access** | ✅ | Only the sign-in, password-reset, health and ROI-calculator pages answer; everything else redirects to sign-in. |
| **Roles, every POST route** | ✅ | Viewers and reviewers are refused (403) everywhere the permission table says so; refusals are written to the audit log (`auth.denied`, QA-027 fix). Approvers may archive quotes and delete extras, charge names, statements, payments and accrual adjustments: consistent with `PERMISSION_MIN_ROLE` (rates and month-end are approver-level) and the README, so not a defect. |
| **GET requests that write** | ✅ | Only first-view creation of default settings rows (`DisputeSettings.for_org` and similar). Harmless. |
| **API: roles, scopes, key states** | ✅ | Every one of the 119 cases answered as documented: missing scope → 403 naming it; read-only key → 403 on upload; revoked, expired, garbage and wrong-secret keys → 401; other company → 404; a session with a bad Bearer key → 401 (no fallback, QA-017 holds). |
| **Session fixation** | ✅ | The session key changes at sign-in; a planted key stays signed out. |
| **Sign-out** | ✅ | The old cookie is dead on the server afterwards; sign-out by GET is refused (405). |
| **Password change** | ✅ | Other sessions of that person are signed out; the current one stays. |
| **Removed member / lowered role / deactivated user** | ✅ | Takes effect on the next request of a live session (404, 403, signed out; API 401). |
| **Idle time-out** | ✅ | A session aged past 30 minutes is signed out. |
| **CSRF** | ✅ | A session-authenticated API upload and a web approve without the token, from a foreign Origin, → 403. |
| **Inbound email webhooks** | ✅ | Postmark without or with wrong basic auth → 401; Mailgun unsigned or forged → 403. |
| **Server-side requests** | ✅ | Slack/Teams alert URLs: 15 hostile forms refused (look-alike hosts, `@` tricks, IPs, ports, http). IMAP hosts: loopback, private, link-local, `localtest.me`, IPv4-mapped IPv6 refused. Webhooks: `localtest.me` and `127.1` pass the syntax check but are refused when saved and before each send (resolved and pinned, `urlguard.check_and_resolve`). |
| **Hostile uploads** | 🟡 | Image decompression bomb: refused cleanly. PDF/ZIP polyglot: treated as a 1-page PDF. Huge page counts and deep nesting: QA-098. |
| **Rate limits** | 🟡 | Sign-in lockout (session 05) and API limit (429 seen in session 18) work; password reset: QA-097. |
| **Secrets in logs** | ✅ | Web, worker and beat logs of sessions 18/21 (3,700 lines): no passwords, API keys, session ids, CSRF tokens or OAuth tokens. The only sensitive text is password-reset links inside emails printed by the console backend (development only). |
| **Dependency vulnerabilities** | ✅ | `pip-audit` over the 83 installed packages: no known vulnerabilities. See QA-100. |

## 3. New issues

**QA-097 — Medium — Password reset has no rate limit: anyone can flood a user's inbox**
25 reset requests for `reviewer@example.com` in one second sent 25 "Set your ShipMatch password" emails. Anyone who knows an address can mail-bomb that user and burn the sender's reputation with the mail provider; the page correctly doesn't reveal whether the address has an account, so this costs the attacker nothing. Limit per address (e.g. 3 per hour) and per IP, silently (same page either way).

**QA-098 — Medium — Small hostile PDFs hold a worker for a long time; no page limit, and OCR would run on every page**
A 2.4 MB PDF of 20,000 blank pages was accepted and took 24.5 s to read (2,000 pages: 2.7 s, so about linear: a 25 MB file holds roughly 200,000 pages and several minutes). A 196 KB PDF with one deeply nested array took 17 s. Only the public try page has a page limit (`TRY_MAX_PAGES`); uploads, email and the API have none, and no Celery time limit is set (session 18), so a few such files tie up every worker. With scan reading on, `ocr.textract_read` renders and sends **every page** to Textract (and the Anthropic reader reads every page), so one scanned 20,000-page upload means 20,000 paid OCR calls. Suggested: a page limit at intake (with a clear message), `CELERY_TASK_SOFT_TIME_LIMIT`/`TIME_LIMIT`, and a per-document page cap for OCR and AI reading.

**QA-099 — Low — Downloading a statement whose stored file is missing gives HTTP 500**
`close/statements/<pk>/file/` raised `FileNotFoundError` (500) when the file was gone from storage. No file view catches a missing file (`statement_file`, `document_file`, `original_file`), so the same is expected for documents (not reproduced). It happens after restoring a database without its media, or when a delete fails halfway: `statement_delete` removes the file *before* deleting the row, so if the row delete fails the record stays and points at nothing (this is how it arose here: the matrix's rolled-back admin delete still removed the file). Answer 404 with "The file for this statement is missing", and delete files after the commit (`transaction.on_commit`).

**QA-100 — Low — Dependencies are not pinned, so production installs untested versions**
`requirements.txt` has only lower bounds (`Django>=5.2.8,<5.3`, `celery>=5.4`, `pypdf>=3.17`…) and there is no lock file: each Docker build installs whatever is newest that day, so production can run versions no test run or vulnerability scan has seen, and two builds of the same commit can differ. Also: the test tools (`pytest`, `pytest-django`) are installed in the production image, and the local environment runs Python 3.14 while the image uses 3.12. Suggested: a lock file (`pip-compile` with hashes) used by the Dockerfile, a separate `requirements-dev.txt` for tests (the file exists), and `pip-audit` in CI.

## 4. Notes (not issues)

- A person removed from their only company gets a bare 404 on the review pages; a page saying "You're no longer a member of a company" would be kinder.
- `/media/` is served without sign-in only when `DEBUG` is on (development); production's Caddy does not serve it.

## 5. Still untested

A real dependency scan of the **production image** (it installs its own versions, QA-100); TLS and headers through Caddy (session 19); brute force across many IPs; the admin site (`/admin/`) beyond its login; OAuth callbacks with forged `state` against real QuickBooks/Xero/Microsoft (session 16).

## 6. Test data left behind

In the throwaway Postgres database: the organization `qa-other` with user `qa_other_admin`, user `qa_viewer` in the demo company, and one sample row each of the objects listed in section 1. In `media/`: the files of documents uploaded through the API during the matrix (the database rows were rolled back, the files stay). The statement file of the Postgres demo was deleted by the matrix (QA-099). `db.sqlite3` and its 148 files were not touched (checked).
