# ShipMatch — AP reconciliation for import shipments

ShipMatch reads shipment paperwork from email (commercial invoices, bills of lading, freight
invoices, credit notes; as PDFs, photos, spreadsheets or ZIP archives), groups each document into the
right shipment, checks the numbers, and posts approved bills and vendor credits to QuickBooks Online or Xero, then
follows whether they get paid. A human approves anything uncertain. Money math and matching are plain
code; AI is used only to read documents.

```
email / upload ──► text (PDF layer or Textract) ──► classify ──► extract (rules or LLM, grounded)
      ──► match to shipment (B/L > container > PO > near-match) ──► validate ──► review & approve
      ──► QuickBooks or Xero bill + PDF attachment (idempotent) ──► payment status read back hourly
```

## Quick start on Windows (no Docker, no API keys)

Needs Python 3.10+ (3.12 recommended). If `py --version` fails, install it first with
`winget install -e --id Python.Python.3.12` (or from python.org, ticking "Add python.exe to PATH"),
then close and reopen the terminal. The `python` command that opens the Microsoft Store is only a
shortcut, not an installed Python. Then, in PowerShell, from the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\quickstart.ps1
```

It creates `.venv`, installs packages, applies database changes, builds the demo data and opens the
server at http://localhost:8000/ (sign in with one of the demo users below). Run it again after
updating the code: it is safe to repeat. Afterwards use the venv's Python directly:

```powershell
.\.venv\Scripts\python manage.py runserver
.\.venv\Scripts\python manage.py run_eval --dataset datasets/synthetic
.\.venv\Scripts\python -m pytest
```

Do not copy `.env.example` to `.env` for this local run: without a `.env` the app uses SQLite,
local files and inline background tasks. Celery workers do not run natively on Windows; use
Docker (below) for Postgres + Redis + workers, or `celery -A config worker --pool=solo` for testing.

### Demo users (local evaluation only)

| Username | Password | Role | What to try |
| --- | --- | --- | --- |
| `admin` | `admin` | Admin of the demo organization | Team, Settings, QuickBooks, API keys, audit log |
| `reviewer` | `reviewer-demo-pass` | Reviewer | Correct values, move documents, accept warnings |
| `approver` | `approver-demo-pass` | Approver, limit USD 50,000 | Override errors with a reason, approve, reject |

Change these passwords (or skip them with `seed_demo --no-demo-users`) on any server other people
can reach.

## Quick start (macOS / Linux)

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo                      # org "demo" and the demo users below (--superuser: admin also gets /admin/)
python manage.py generate_dataset --out datasets/synthetic --shipments 20 --seed 42 --scanned 3
python manage.py ingest_folder datasets/synthetic --org demo
python manage.py runserver                      # http://localhost:8000/
```

Score the pipeline against ground truth:

```bash
python manage.py run_eval --dataset datasets/synthetic
```

Run the tests:

```bash
pytest
```

## With Docker (Postgres, Redis, background workers)

Windows, with Docker Desktop running (stop `manage.py runserver` first, it uses the same port):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\docker-up.ps1
```

macOS / Linux: `docker compose up -d --build`, then `docker compose exec web python manage.py seed_demo`.

The stack runs Postgres, Redis, the web app and two Celery workers. Keys come from `.env`; the
database, broker and cache settings are fixed in `docker-compose.yml`, so `.env` only needs your
provider keys. Stop with `docker compose down` (data is kept) or `docker compose down -v` (wipes it).
S3-compatible storage (MinIO) is optional: `docker compose --profile s3 up -d` and set `S3_*` in `.env`.

Production: `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`
(gunicorn + Caddy HTTPS, uploaded PDFs in a named volume). Needs `DJANGO_SECRET_KEY`,
`FIELD_ENCRYPTION_KEY`, `POSTGRES_PASSWORD` and `DOMAIN` in `.env`. Nightly backups: `deploy/backup.sh`.
For a single server with everything set up for you, see [Hosting](#hosting).

## Hosting

One Ubuntu server (for example a 2 GB DigitalOcean droplet) runs the whole stack behind Caddy with
automatic HTTPS. From the project folder on macOS, Linux or WSL:

```bash
DOMAIN=shipmatch.example.com ./scripts/deploy-droplet.sh push root@<droplet-ip>
```

The script installs Docker, opens only ports 22, 80 and 443, creates `/opt/shipmatch/shared/.env`
from `.env.example` with generated secrets (never printed), starts `docker-compose.prod.yml`, and
sets up nightly `pg_dump` and file backups with 14-day rotation. Run the same command to update;
`scripts/deploy-droplet.sh rollback` (on the server) returns to the previous release, and
`restore-db` restores the backup taken before an update. Register
`https://<domain>/accounting/qbo/callback` as the QuickBooks redirect URI and
`https://<domain>/accounting/xero/callback` as the Xero redirect URI. Step by step, including a
Windows path without rsync: [deploy/digitalocean.md](deploy/digitalocean.md).

`docker-compose.prod.yml` needs Docker Compose 2.24 or newer: it uses `!reset`/`!override` so the
development ports (Postgres 5432, Redis 6379, 8000) and the source mount from `docker-compose.yml`
are really removed in production instead of being merged in.

## Supported inputs

Upload form, API, email (Gmail) and `ingest_folder` all accept the same files, through one shared function
(`ingest_bytes`). A file's type is decided by its content, not its name.

| You send | What ShipMatch does |
| --- | --- |
| PDF (text or scanned) | Read from its text layer; scans go through OCR (Claude, or Textract). |
| Photo or scan: JPG, PNG, TIFF, WebP | Turned upright from the camera's orientation and converted to a PDF (a multi-page TIFF gives one page per frame). The text is read by OCR; without OCR the document waits as "Needs OCR" for someone to type the B/L, container or PO number. The original image can be downloaded. |
| Spreadsheet: XLSX, CSV | Read cell by cell into a text form that keeps rows and columns, and drawn as a readable PDF copy for the review screen and QuickBooks. Without AI, a table reader finds the header row and the usual columns (description, qty, rate, amount, container, B/L, invoice number and date, vendor, PO) and the label/value cells around them; the total comes from the sheet's own total row. Old `.xls` files are refused with a request to save them as `.xlsx`. |
| ZIP archive | Every supported file inside becomes its own document ("From archive ..." on each). `__MACOSX` folders and hidden files are ignored; unsupported, password-protected or unsafe entries are skipped and listed. The archive's page lists what happened to every file and offers the ZIP for download. |
| PDF with several invoices (a carrier batch) | Split into one document per invoice, each read and matched on its own. Page boundaries come from the text: the invoice number changes, "Page 1 of N" starts again, a new invoice title, a total that ends the previous page. A page with the same invoice number, "Page 2 of 3" or "continued" is never a boundary, so a long invoice is never split. With AI on, one small AI call checks uncertain boundaries. The original stays as the parent; **Keep as one document** on its page undoes a wrong split. |
| Credit note | Its own document type: credit note number, the invoice it credits, a positive credit amount. It joins its shipment by B/L, container or PO, or else through the invoice it credits. Checks: the credited invoice wasn't received, sits in another shipment or is smaller than the credit; the same credit note twice. Shipment totals are net of credits, and approved credit notes are posted to QuickBooks as Vendor Credits (idempotent, like bills). |

Refused with a message that says what to do: HEIC photos (export as JPG), Word, PowerPoint and OpenDocument files
(save as PDF or XLSX), RAR and 7-Zip archives (use ZIP), empty files, images too small to be a page (signature logos),
and anything over the limits below. Email signature images embedded in the message body are not imported.

| Setting | Default | Meaning |
| --- | --- | --- |
| `INTAKE_MAX_FILE_MB` | 25 | Largest PDF, image or spreadsheet |
| `INTAKE_MAX_ARCHIVE_MB` | 100 | Largest ZIP as received |
| `INTAKE_ZIP_MAX_FILES` | 200 | Files inside one ZIP, nested archives included |
| `INTAKE_ZIP_MAX_TOTAL_MB` | 300 | Everything inside one ZIP, unpacked (checked again while reading, since ZIP headers can lie) |
| `INTAKE_ZIP_MAX_RATIO` | 100 | How much tighter than its real size a file may be packed; more looks like a zip bomb and the archive is refused |
| `INTAKE_ZIP_MAX_DEPTH` | 1 | A ZIP inside a ZIP is opened; one level deeper is skipped |
| `INTAKE_MAX_IMAGE_MEGAPIXELS` | 120 | Largest photo |
| `INTAKE_MAX_IMAGE_PAGES` | 50 | Pages in one multi-page TIFF |
| `INTAKE_SHEET_MAX_ROWS` / `INTAKE_SHEET_MAX_COLUMNS` | 5000 / 60 | Rows and columns read per sheet |
| `INTAKE_SPLIT_PDFS` | 1 | Split multi-invoice PDFs |
| `INTAKE_SPLIT_AI_CONFIRM` | 1 | With AI reading on, confirm uncertain page boundaries with one small AI call |

Known limits: formulas in an `.xlsx` are shown with the value Excel saved; a workbook written by a program
that never calculated its formulas shows those cells empty (open and save it in Excel first). Hidden sheets
are not read. A ZIP's contents are never written to disk.

`python manage.py try_extraction` accepts photos and spreadsheets too.

## Pilot with your own documents

1. Put 10 to 20 real PDFs (invoices, bills of lading, freight invoices; remove sensitive details first)
   in a folder named `real_docs` inside the project. The folder is never committed or copied into images.
2. In `.env`: `EXTRACTION_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=...`, then restart.
3. Check one file first: `python manage.py try_extraction real_docs\some-invoice.pdf`
   (shows each value, whether it was found word for word in the document, tokens and cost).
4. Import everything into a separate "pilot" organization: `python manage.py pilot`
   (with Docker: `docker compose exec web python manage.py pilot`).
5. Review and approve the pilot shipments in the app (switch organization in the sidebar).
   **Reports > Reading accuracy** then shows how often each field was read correctly, measured from
   what reviewers changed. `python manage.py accuracy_report --org pilot` prints the same.
6. For a labelled benchmark, write `real_docs/ground_truth.json` in the same format as
   `datasets/synthetic/ground_truth.json` and run `python manage.py run_eval --dataset real_docs`
   (add `--provider rules` or `--llm-input pdf` or `--model claude-haiku-4-5` to compare set-ups).

## Vendor learning

ShipMatch learns from reviewers. When someone corrects a value and that value is printed in the
document, it remembers, for that vendor and document type:

* the label printed before the value (an invoice number after "Ref No." instead of "Invoice No."),
* whether the vendor writes numeric dates day first (03/08/2026 = 3 August) or month first,
* the last corrections ("we read A, the correct value was B").

The vendor's next documents are recognized from the top of the text (fuzzy match against known
vendor names, no AI call). With the rule reader, values after the learned labels fill missing fields
or replace values the rules read differently, and ambiguous dates are read in the vendor's order.
With the AI reader, a short "vendor notes" block (labels, date order, up to 3 recent corrections,
never longer than `LEARNING_HINT_MAX_CHARS`) is added to the prompt. The review screen says
"Learned from N corrections for this vendor" and lists the values learning filled; **Settings >
Vendor learning** shows what was learned per vendor and how many documents it helped, with
**Forget** (recorded in the audit log). Turn it off with `VENDOR_LEARNING=0`.

## QuickBooks sandbox

1. At developer.intuit.com create an app with the **Accounting** scope. Under *Keys and credentials
   (Development)* copy the client ID and secret into `.env` (`QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`,
   `QBO_ENVIRONMENT=sandbox`) and add the redirect URI `http://localhost:8000/accounting/qbo/callback`.
2. Restart, then **Settings > Accounting > Connect QuickBooks** and choose your sandbox company.
   The same page shows the redirect and reconnect URLs to paste into the Intuit app.
3. Choose a default expense account, approve a shipment and click **Post bills to QuickBooks**.
4. Check from the command line: `python manage.py qbo_check --org demo --post SHP-000010`.

Posting is idempotent (each bill carries a `requestid`), handles Intuit throttling, rotates refresh
tokens safely across workers, and asks an admin to reconnect when Intuit rejects the connection.
Foreign-currency invoices need multicurrency turned on in QuickBooks.

## Xero

An organization posts to QuickBooks Online **or** Xero: one system receives its bills. **Settings >
Accounting** (admins) shows both, with the connection details, default expense account and the app
settings to copy into the Intuit and Xero portals. Connecting one system disconnects the other, but only
once the new one is fully connected; bills already in the old system stay there. The approve button then
says **Post bills to Xero**.

1. At developer.xero.com create an app. A **Web app** has a client secret: put `XERO_CLIENT_ID`,
   `XERO_CLIENT_SECRET` and `XERO_REDIRECT_URI` in `.env`. For a **PKCE** app (no secret) leave
   `XERO_CLIENT_SECRET` empty and ShipMatch signs in with PKCE (S256). Add the redirect URI
   `http://localhost:8000/accounting/xero/callback` (production: `https://<domain>/accounting/xero/callback`).
2. Scopes: ShipMatch asks for `XERO_SCOPES`, by default `offline_access accounting.transactions
   accounting.contacts accounting.settings accounting.attachments`. **Xero apps created from 2 March 2026
   must use granular scopes** instead of `accounting.transactions`; for those set
   `XERO_SCOPES=offline_access accounting.invoices accounting.payments.read accounting.contacts accounting.settings accounting.attachments`.
   If Xero refuses the scopes, the page says so.
3. Restart, then **Settings > Accounting > Connect Xero**. If the sign-in reaches several Xero
   organisations, choose the one to post to (changing it later clears the Xero contacts and account codes
   learned for the old one). Choose the default expense account (expense, direct cost and overhead
   accounts that have a code) and whether new bills are created as **drafts** (default; someone approves
   them in Xero) or **approved** (awaiting payment).
4. Approve a shipment and click **Post bills to Xero**. Check from the command line:
   `python manage.py xero_check --org demo [--post SHP-000010] [--payments]`.

What ShipMatch sends: each invoice becomes a bill (`Invoices`, `Type=ACCPAY`) with one line per invoice
line on the vendor's account code (an adjustment line makes it equal the printed total), the supplier's
invoice number in `InvoiceNumber` (Xero shows it as the bill's Reference), date, due date, currency,
`LineAmountTypes=NoTax` (amounts as printed; adjust tax in Xero if you reclaim it), a "Go to ShipMatch"
link when `SITE_URL` is https, the PDF attached (`PUT /Invoices/{id}/Attachments/{file}`) and a history note
naming the shipment. Credit notes become supplier credit notes (`CreditNotes`, `Type=ACCPAYCREDIT`).
The vendor's contact is found by name (archived ones too, merged contacts followed) or created with the
invoice's currency; an archived contact stops posting with a message to restore it.

| Situation | What happens |
| --- | --- |
| Retry, double click, worker restart | Every write carries an `Idempotency-Key`; a bill created before a failure is never created again, and after a long gap ShipMatch looks for the same contact, number and amount before creating one |
| Access token expired (30 minutes) or refused (401) | Refreshed once with the rotating refresh token, under a row lock so two workers never spend the same token |
| Refresh token rejected (`invalid_grant`, 60 days unused, or revoked) | Connection marked "needs reconnecting", one audit row, "Xero needs reconnecting" alert |
| 429, minute limit | Waits for `Retry-After` (up to a minute) and retries |
| 429, daily limit | Stops the run and says when it can continue; payment checks pause until then |
| Validation error | Xero's `ValidationErrors` shown with a plain explanation (unknown or archived account code, currency not enabled, missing due date for approved bills, lock date) |
| Invoice in a currency Xero doesn't have | Blocked: add the currency in Xero (Settings > Currencies) |
| Disconnect | The refresh token is revoked at `identity.xero.com/connect/revocation` |
| Public demo (`DEMO_MODE` without `DEMO_SEND_OUTSIDE`) | Nothing is sent to Xero; demo accounts can't disconnect it |

Vendor account rules ("Expense account for this vendor" on an invoice) are kept per system: a QuickBooks
account ID and a Xero account code can both be saved for the same vendor.

`apps.accounting.services.posting.register_line_builder(fn)` lets another app decide the lines of a bill
(for example one line per shipment when an invoice covers several): `fn(doc)` returns a list of
`{"description", "amount", "memo", "class", "tracking"}` dicts (or `(description, amount, memo)` tuples),
or `None` to keep the default lines. `memo` is added to the line text, `class` becomes a QuickBooks
ClassRef and `tracking` Xero tracking categories. Lines that don't add up to the printed total get an
adjustment line, for both systems.

| Variable | Default | Purpose |
| --- | --- | --- |
| `XERO_CLIENT_ID`, `XERO_CLIENT_SECRET` | empty | The Xero app (no secret = PKCE app) |
| `XERO_REDIRECT_URI` | `http://localhost:8000/accounting/xero/callback` | Must match the Xero app |
| `XERO_SCOPES` | `offline_access accounting.transactions accounting.contacts accounting.settings accounting.attachments` | Use granular scopes for apps created from March 2026 |

## Payment status

ShipMatch reads back whether posted bills were paid, so AP can see it without opening the accounting system.

* **What is read**: QuickBooks `Bill` Balance and TotalAmt, the linked `BillPayment` (date, amount, number)
  and vendor credits applied in it, and `VendorCredit` Balance; Xero `Invoices` Status, AmountDue,
  AmountPaid, AmountCredited, DueDate, FullyPaidOnDate and payments, and credit notes' RemainingCredit and
  allocations. Each posted bill gets a status (unpaid, partly paid, paid, voided or deleted in the
  accounting system), amounts, due date, paid date and the payments; "overdue" is an unpaid or partly paid
  bill past its due date. Credits show as not used yet, partly used or used in full.
* **When**: Celery beat runs `apps.accounting.tasks.sync_all_payments` every hour; each organization is read
  at most once per `PAYMENT_SYNC_HOURS` (minimum 1). Approvers and admins can press **Check payments now**
  (dashboard, shipment page, Settings > Accounting). One check per organization runs at a time.
* **Batched**: QuickBooks `select * from Bill where Id in (...)` (100 per query, then the bill payments and
  vendor credits the same way); Xero `Invoices?IDs=...` (40 per call, every status so voided and deleted
  bills are reported) and credit notes by ID. Unpaid bills are read every time; paid ones again weekly for
  90 days in case a payment is reversed. A used-up Xero daily allowance pauses the checks until Xero allows
  calls again. Only bills posted to the connected company or organisation are read.
* **Voided or deleted** in the accounting system: a warning on the shipment, an audit row and the "Bill
  voided or deleted in accounting" alert. Every status change is in the shipment's activity and the audit log.
* **Where it shows**: a Payments card on the shipment page (with links into QuickBooks or Xero), a Payments
  column and filter (not paid yet, overdue, paid, voided or deleted, not checked yet) in the review queue, and
  "Unpaid bills by age" on the dashboard: not due yet, 1 to 30, 31 to 60, 61 to 90 and over 90 days past due,
  in the home currency (bills in a currency without an exchange rate in Settings are listed apart), with
  vendor credits not used yet.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PAYMENT_SYNC_ENABLED` | 1 | Turn the scheduled check off with 0 (the button still works) |
| `PAYMENT_SYNC_HOURS` | 1 | Hours between scheduled checks per organization (at least 1) |

## Email intake

Documents arrive by email with no uploading, whatever mail system the client uses. **Settings > Email
intake** (admins) shows every way in, each mailbox's health and the last 30 emails with what happened to
every attachment. Each email is stored once per organization (by Message-ID), so retries, re-checks and
the same email arriving through two mailboxes never import anything twice; identical files are linked
to the document that already exists. Every attachment goes to the normal import, which decides which file
types it accepts; skipped ones are listed with the reason. Pictures inside the email text (signature
logos) and tiny images are skipped. Each mailbox can be limited to an allowed sender list. Documents show
the email they came from (sender, subject, mailbox, the other attachments).

| Way in | Best for | Setup |
| --- | --- | --- |
| Forwarding address | Everyone: an Outlook rule or Gmail filter forwards supplier emails | `INBOUND_EMAIL_DOMAIN` + Postmark or Mailgun (below) |
| Microsoft 365 / Outlook.com | Reading a mailbox directly when forwarding outside the company is blocked | Azure app registration (below), then **Connect Microsoft 365** |
| IMAP | Google Workspace, Yahoo, iCloud, Zoho, hosting providers | Host, port, user, app password in the page; **Test connection** says exactly what is wrong |
| Gmail label | The existing Gmail API connection | `python manage.py gmail_auth --org <slug>`; it then shows as a mailbox |

Mailboxes are checked every `MAILBOX_POLL_MINUTES` (Celery beat task
`apps.mailboxes.tasks.poll_all_mailboxes`; one failing mailbox never stops the others), or straight away
with **Check now** or `python manage.py poll_mailboxes [--org demo] [--sync]`. A mailbox whose sign-in is
rejected is marked "needs reconnecting" and skipped until an admin fixes it. Without Redis/Celery
(`CELERY_TASK_ALWAYS_EAGER=1`) run `poll_mailboxes` from a scheduler (cron or Windows Task Scheduler).

**Forwarding address.** Each organization gets `<org-slug>-<16 random characters>@INBOUND_EMAIL_DOMAIN`
(Settings > Email intake has a copy button, forwarding steps for Outlook and Gmail, and **New address** to
replace a leaked one; the old address stops working at once). Gmail's forwarding confirmation code shows
under Recent emails. Point the domain's MX records at one provider:

* Postmark: in the server's inbound stream set the inbound domain and the webhook URL
  `https://USER:PASSWORD@your-host/inbound/email/postmark/` with `POSTMARK_INBOUND_USER` and
  `POSTMARK_INBOUND_PASSWORD` from `.env`. DNS: `MX 10 inbound.postmarkapp.com`.
* Mailgun: add the receiving domain (`MX mxa.mailgun.org`, `mxb.mailgun.org`), create a route
  `match_recipient(".*@in.example.com")` with `forward("https://your-host/inbound/email/mailgun/")`, and set
  `MAILGUN_SIGNING_KEY` to the HTTP webhook signing key. Requests are verified (HMAC-SHA256 of
  timestamp + token), must be under 15 minutes old and each token is accepted once.

The webhooks answer 200 when an email is stored (or was stored before), 401/403 for bad credentials or
signatures, 429 with `Retry-After` when one organization sends more than `INBOUND_EMAIL_RATE_PER_MINUTE`,
5xx on errors (the provider retries), and the provider's "don't retry" code (Postmark 403, Mailgun 406)
for unknown or paused addresses, oversized emails (`INBOUND_EMAIL_MAX_MB`) and stale or replayed requests.
Unknown and paused addresses get the same reply, so nobody can probe which addresses exist.

**Microsoft 365: Azure app registration** (once per ShipMatch installation)

1. In the Azure portal open **Microsoft Entra ID > App registrations > New registration**. Name it
   ShipMatch. Supported account types: *Accounts in any organizational directory and personal Microsoft
   accounts* (keep `MS_TENANT=common`), or *this organizational directory only* and set `MS_TENANT` to
   the directory (tenant) ID.
2. Redirect URI: platform **Web**, `http://localhost:8000/settings/email/microsoft/callback` (production:
   `https://your-host/settings/email/microsoft/callback`). Put the same value in `MS_REDIRECT_URI`.
3. **Certificates & secrets > New client secret**: copy the secret's *Value* into `MS_CLIENT_SECRET`. Note
   the expiry date and create a new secret before it runs out.
4. **Overview**: copy the *Application (client) ID* into `MS_CLIENT_ID`.
5. **API permissions > Add a permission > Microsoft Graph > Delegated**: `offline_access`, `User.Read`,
   `Mail.ReadWrite`. If the client's tenant doesn't let users consent to mail access, their admin clicks
   **Grant admin consent** (or approves ShipMatch when the first user connects).
6. Restart ShipMatch, then **Settings > Email intake > Connect Microsoft 365** and sign in with the mailbox
   to read. Choose the folder, and whether imported emails get the "ShipMatch" category and/or move to a
   folder.

ShipMatch reads the signed-in user's own mailbox. For a shared AP mailbox, sign in as a user mailbox that
receives the emails, or forward from the shared mailbox with a rule. Tokens are stored encrypted
(`FIELD_ENCRYPTION_KEY`), refresh tokens rotate, and Graph throttling (429 with `Retry-After`) is honoured.

**IMAP notes.** Gmail/Google Workspace, Yahoo and iCloud need an app password. Microsoft 365 no longer
accepts IMAP passwords, so use Connect Microsoft 365. Emails are read with `BODY.PEEK` (left unread
unless "mark as read" is ticked) and tracked by UID; if the server renumbers the folder (UIDVALIDITY
changes) the last week is read again without creating duplicates. Hosts on private networks are refused
unless `MAILBOX_ALLOW_PRIVATE_HOSTS=1`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `INBOUND_EMAIL_DOMAIN` | empty (off) | Domain of the forwarding addresses |
| `POSTMARK_INBOUND_USER`, `POSTMARK_INBOUND_PASSWORD` | empty | HTTP basic auth for the Postmark webhook |
| `MAILGUN_SIGNING_KEY` | empty | Mailgun HTTP webhook signing key |
| `INBOUND_EMAIL_MAX_MB` | 40 | Largest webhook request accepted |
| `INBOUND_EMAIL_RATE_PER_MINUTE` | 60 | Emails per organization per minute before asking the provider to retry |
| `MS_CLIENT_ID`, `MS_CLIENT_SECRET` | empty | Azure app registration |
| `MS_TENANT` | `common` | `common`, `organizations` or a tenant ID |
| `MS_REDIRECT_URI` | `http://localhost:8000/settings/email/microsoft/callback` | Must match the Azure app |
| `MAILBOX_POLL_MINUTES` | 5 | How often mailboxes are checked |
| `MAILBOX_ALLOW_PRIVATE_HOSTS` | 0 | Allow IMAP servers on private networks (testing only) |
## Demo mode

For a server you hand to prospects, set `DEMO_MODE=1`:

* a slim banner on every page: "Demo environment. Data resets every night.";
* the sign-in page lists the demo accounts with their passwords and a **Use this account** button
  (passwords appear only in demo mode);
* demo accounts can try everything except what would spoil the demo for the next visitor: changing
  their passwords or two-factor, removing team members, changing the demo accounts' roles,
  disconnecting QuickBooks, changing maker-checker or required two-factor, the platform admin site,
  and invitations when real email is configured. They get a friendly message instead. Demo accounts
  never act as platform superusers, so they can't see other organizations;
* `python manage.py reset_demo` deletes the organizations in `DEMO_ORGS` and rebuilds them: demo
  users with their published passwords, a fresh synthetic document set (read with the free rule
  reader unless `--use-ai`), a vendor learning example (a customs broker invoice corrected by the
  reviewer, and a second one read correctly because of it), and exchange rates when the `seed_rates`
  command is installed. Celery beat runs it every night at `DEMO_RESET_HOUR` (UTC). Without
  `DEMO_MODE=1` it refuses to run unless you add `--force`;
* nothing leaves the server: anyone can sign in with the shared accounts, so dispute emails, alert
  tests, digests and Slack or Teams alerts are never sent (emails are logged instead, alerts show
  "Not sent: this is the public demo"). Sending a dispute still moves it to "sent" so the whole flow
  can be shown. Set `DEMO_SEND_OUTSIDE=1` only on a private demo where every visitor is trusted.

## Try page

`TRY_ENABLED=1` opens `/try/`, where anyone can upload one PDF without signing in (default limits
10 MB and 10 pages: `TRY_MAX_MB`, `TRY_MAX_PAGES`) and see the document type, every value read and
the document checks, with a call to action linking to `DEMO_CONTACT_URL`.

* Each upload is read in its own new sandbox organization, never a customer's, so visitors never
  see each other's documents (duplicate and matching checks only see that one file).
* The result is only reachable through an unguessable link; only a hash of the token is stored, and
  result pages are `noindex` and `no-store`, and never send their address to other sites.
* Abuse controls: per-IP limits per hour and per day (`TRY_RATE_PER_HOUR`, `TRY_RATE_PER_DAY`, in
  the cache), a global daily cap (`TRY_DAILY_LIMIT`) that bounds AI cost, a honeypot field and CSRF.
* Files, results and the sandbox are deleted after `TRY_RETENTION_HOURS` (default 24) by
  `python manage.py purge_try`, which Celery beat runs every hour, or straight away with
  **Delete my document now**. The page explains this to visitors.
* PDFs only, like the rest of intake. Scanned PDFs are read when OCR is configured.

## Self-serve signup

`SIGNUP_ENABLED=1` opens `/signup/` (linked from the sign-in page and the public pages): company name, your
name, work email and a password checked by Django's password validators (at least 12 characters, not common,
not only numbers, not like the name or email).

1. Nothing is created yet. The sign-up waits (password already hashed) and a confirmation link is emailed. The
   link is signed, works for `SIGNUP_VERIFY_HOURS` (48) and only once. Opening it shows a **Confirm** button,
   so mail scanners that open links don't create accounts.
2. Confirming creates the user (username = email), the organization (its address name, the slug, is made from
   the company name: `acme`, then `acme-2`, ...; reserved words and demo slugs are avoided), the admin
   membership, a `BILLING_TRIAL_DAYS` free trial and the getting-started checklist, in one transaction. The
   person is signed in and lands on the dashboard; the organization's time zone comes from their browser.
3. An expired link says so and sends a new one; a used link says the account is ready and links to sign-in.

Abuse controls: a honeypot field (a filled one looks like success, nothing is stored or sent), per-IP limits per
hour and day (`SIGNUP_RATE_PER_HOUR`, `SIGNUP_RATE_PER_DAY`; IPv6 counted per /64), at most 3 verification
emails per address per hour, throwaway email domains refused (a short list in `apps/billing/signup.py`;
`SIGNUP_BLOCK_DISPOSABLE=0` turns it off, `SIGNUP_BLOCKED_DOMAINS` adds more), and CSRF. An address that
already has an account gets a "you already have an account" email instead, and the visitor sees the same
"check your email" page, so nobody can find out which addresses are registered. Unconfirmed sign-ups are
deleted a day after their link expires (`purge_signups`, hourly in Celery beat).

**Getting started.** Organizations made by sign-up show admins a checklist on the dashboard: connect accounting,
set up email intake, upload the first documents, invite the team. Each step ticks itself from what really
exists (a working QuickBooks connection, a connected mailbox or an email received, a document, a second
member), never from a click. It disappears when everything is done or an admin hides it. Another accounting
integration adds its own check with `apps.billing.onboarding.register_accounting_check(fn)`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SIGNUP_ENABLED` | 0 | Open `/signup/` |
| `SIGNUP_VERIFY_HOURS` | 48 | How long the emailed link works |
| `SIGNUP_RATE_PER_HOUR` / `SIGNUP_RATE_PER_DAY` | 5 / 20 | Attempts per visitor network |
| `SIGNUP_BLOCK_DISPOSABLE` | 1 | Refuse throwaway email domains |
| `SIGNUP_BLOCKED_DOMAINS` | empty | More domains to refuse, comma separated |

Sign-up needs working email (`EMAIL_HOST`). On a public demo emails are logged, not sent, so keep it off there.

## Billing

`BILLING_ENABLED=1` adds **Settings > Billing** (admins) with Stripe. Without it there are no plans, limits or
Stripe calls at all. Organizations an operator set up (no trial, no subscription) are billed by agreement and
have no limits until their admin subscribes.

**Plans** are in `BILLING_PLANS` (`config/settings.py`): Starter, Growth and Scale, each with a monthly Stripe
price id from the environment, documents per month, users (0 = no limit) and features. Create the three
products with monthly prices in Stripe and put the price ids in `STRIPE_PRICE_STARTER`, `STRIPE_PRICE_GROWTH`,
`STRIPE_PRICE_SCALE`. A sign-up starts a `BILLING_TRIAL_DAYS` trial with the users and features of
`BILLING_TRIAL_PLAN` and `BILLING_TRIAL_DOCUMENTS` documents. Outgoing webhooks need a plan with the
`webhooks` feature; inviting someone past the plan's users is refused with the reason.

**Paying.** *Choose a plan* creates the Stripe customer (once) and a Checkout session for a monthly subscription
(during the trial the first charge waits until the trial ends). Coming back from Checkout reads the session and
subscription from Stripe straight away; webhooks keep it in sync afterwards. With a live subscription, plan
changes, cards, invoices and cancelling go through **Payment method and invoices** (the Stripe Customer
Portal), so an organization never has two subscriptions. Every call is plain HTTPS to Stripe's REST API with
httpx (form-encoded, idempotency keys, no Stripe SDK); nothing is sent on a public demo.

**Webhooks.** In Stripe, add an endpoint `https://<your host>/billing/stripe/webhook/` with the events
`checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`,
`customer.subscription.deleted`, `customer.subscription.paused`, `customer.subscription.resumed` and
`invoice.payment_failed`, and put its signing secret in `STRIPE_WEBHOOK_SECRET` (several, comma separated,
while you roll it). Each request is checked: HMAC-SHA256 of `timestamp.body`, any `v1` signature against any
secret, and the timestamp within `STRIPE_WEBHOOK_TOLERANCE` seconds (a replayed old request is refused). Every
event id is processed once; if processing fails, nothing is kept and Stripe retries.

**Subscription status** on the organization: trialing, active, past_due (Stripe is retrying a payment; nothing
stops, admins get an email and everyone a banner), canceled. Events that arrive late or twice never move it
backwards, a canceled subscription never comes back to life, and events about an older subscription never
override a newer one. A trial that ended without a plan, or a canceled subscription, pauses new documents.

**Usage** counts documents received in the billing month (Stripe's period; the trial's own dates; otherwise the
calendar month in the organization's time zone): each file in a ZIP and each invoice of a split PDF counts once,
the ZIP or batch itself and files received twice don't. The billing page shows the meter.

* At 100% of the plan: a banner for everyone and one email to the admins per month. Nothing stops.
* At `BILLING_HARD_LIMIT_PERCENT` (120%): new uploads, emails and API uploads (HTTP 402) are refused with a
  message saying why, until when, and what to do. Email attachments refused this way are listed with the reason
  under Settings > Email intake; upload them again after changing the plan. A ZIP that starts below the limit is
  imported whole.
* Never blocked: reviewing, correcting, approving, rejecting and posting what is already in.

| Variable | Default | Meaning |
| --- | --- | --- |
| `BILLING_ENABLED` | 0 | Plans, limits and Settings > Billing |
| `STRIPE_SECRET_KEY` | empty | Secret key (`sk_live_...` or `sk_test_...`) |
| `STRIPE_WEBHOOK_SECRET` | empty | Webhook signing secret(s), comma separated |
| `STRIPE_WEBHOOK_TOLERANCE` | 300 | Seconds a signed webhook stays valid |
| `STRIPE_PRICE_STARTER` / `_GROWTH` / `_SCALE` | empty | Monthly price ids |
| `BILLING_STARTER_PRICE` / `_DOCUMENTS` (and GROWTH, SCALE) | 199/300, 499/1000, 1290/4000 | Shown price, monthly documents |
| `BILLING_CURRENCY` | USD | Shown with the prices |
| `BILLING_TRIAL_DAYS` / `BILLING_TRIAL_PLAN` / `BILLING_TRIAL_DOCUMENTS` | 14 / growth / 100 | The free trial |
| `BILLING_HARD_LIMIT_PERCENT` | 120 | Where new documents stop |
| `STRIPE_API_BASE`, `STRIPE_API_VERSION` | api.stripe.com, 2024-06-20 | For a proxy or a pinned API version |

Built against Stripe's documented REST API and tested with a fake Stripe (`httpx.MockTransport`); try it with
test keys and `stripe listen --forward-to localhost:8000/billing/stripe/webhook/` before going live.

## Exports, webhooks and API

**Exports.** The review queue and the Documents page have an **Export** menu (anyone who can view): shipments
with totals per currency and in the home currency, credits, open errors and warnings, money at risk, the open
issues, approval and posting (QuickBooks ids and errors; payment columns too when another feature adds them);
the issues of those shipments; documents with every value read, which values people corrected and which to
check. The current tab and search apply. CSV is streamed (UTF-8 with BOM for Excel) and Excel is written row
by row (`EXPORT_XLSX_MAX_ROWS`), so large organizations export without loading everything in memory. Every row
goes through `apps.core.csvsafe`, and Excel cells are always text, never formulas. Each export is in the audit
log with its filters. URL: `/exports/<shipments|documents|issues>/?format=csv|xlsx&status=...&q=...`.

**Outgoing webhooks** (Settings > Webhooks, admins; Growth and Scale plans when billing is on). Add an https
address and choose events: `document.received`, `document.extracted`, `shipment.needs_review`,
`shipment.ready`, `shipment.approved`, `shipment.rejected`, `bill.posted`, `bill.failed` (vendor credits too,
with `kind`), `issue.created` (each new problem once, even though checks re-run), and `dispute.sent`,
`dispute.credit_received`, `dispute.resolved`, `dispute.closed`, `dispute.overdue`. Each is a `POST`:

```json
{"id": "evt_3f2a...", "type": "shipment.approved", "created": "2026-10-03T09:15:02Z", "organization": "acme",
 "api_version": "2026-10-01", "data": {"object": {"object": "shipment", "reference": "SHP-000042",
 "status": "approved", "bl_number": "MSCU1234567", "totals": {"USD": "18240.00"}, "url": "https://..."}}}
```

Summaries carry references, statuses, amounts and a link, never secrets, document text or files. Headers:
`ShipMatch-Event-Id`, `ShipMatch-Event-Type`, `ShipMatch-Delivery-Id`, `ShipMatch-Timestamp` and
`ShipMatch-Signature: v1=<hex>`, where the signature is HMAC-SHA256 of `<timestamp>.<raw body>` with the
endpoint's secret (`whsec_...`, shown once; **New secret** signs with both the new and the old one for
`WEBHOOK_SECRET_OVERLAP_HOURS`). Checking it in the receiving system (Python; the same idea in any language):

```python
import hashlib, hmac, time

def from_shipmatch(secret: str, body: bytes, timestamp: str, signature: str, tolerance=300) -> bool:
    if abs(time.time() - int(timestamp)) > tolerance:
        return False                                       # old request: possibly replayed
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, part.strip()[3:])
               for part in signature.split(",") if part.strip().startswith("v1="))
```

Use the raw body, before parsing JSON, and de-duplicate on the event `id`: retries and replays send the same id.

Delivery: only https on port 443 or 8443, no user names in the address, and the host must resolve to public
addresses only (private, loopback, link-local, carrier-grade NAT and IPv4-mapped addresses are refused), checked
when saved and again before every request. The connection goes to the address that was checked (host name kept
for TLS and `Host`), so a DNS change can't redirect it inside your network; redirects are never followed;
`WEBHOOK_TIMEOUT_SECONDS` per request; webhooks connect directly, not through `HTTPS_PROXY`. Anything but 2xx is
retried with backoff (30 s, 2 min, 10 min, 30 min, 1 h, 3 h, 6 h; `Retry-After` respected) up to
`WEBHOOK_MAX_ATTEMPTS`. After `WEBHOOK_DISABLE_AFTER_FAILURES` failed attempts in a row the endpoint is turned
off, its waiting deliveries stop and the admins get an email; turning it on again resets it. The delivery log
shows each answer's code, time and the first part of its body, the exact body sent, and **Replay**. **Send test
event** checks an endpoint at once. Nothing is sent on a public demo. Celery runs deliveries; beat runs
`retry_due_deliveries` every minute (`python manage.py send_webhooks` does the same once). Other apps add event
types with `apps.integrations.events.register_webhook_event()`.

**API.** `/api/docs` documents everything. Keys (Settings > API keys) now have scopes: `shipments:read`,
`documents:read`, `exports:read`, and `documents:write` for keys with upload access. Keys created before scopes
keep every scope their access level allowed. A request outside a key's scopes gets 403 naming the scope.

| Endpoint | Scope | Returns |
| --- | --- | --- |
| `GET /api/{org}/shipments?status=&q=&limit=&offset=` | shipments:read | Shipments with open error and warning counts |
| `GET /api/{org}/shipments/{id}` | shipments:read | Documents, issues (title, money at risk), totals per currency and in the home currency |
| `GET /api/{org}/documents?view=&type=&status=&q=&received_from=&received_to=` | documents:read | Documents with every value read |
| `GET /api/{org}/documents/{id}` | documents:read | One document |
| `GET /api/{org}/exports/{shipments,documents,issues}?format=csv\|xlsx&...` | exports:read | The export files, same filters as the web |
| `POST /api/{org}/documents` | documents:write | Upload; 402 when the plan has paused new documents |

| Variable | Default | Meaning |
| --- | --- | --- |
| `WEBHOOK_MAX_ATTEMPTS` | 8 | Tries per delivery |
| `WEBHOOK_DISABLE_AFTER_FAILURES` | 20 | Failed attempts in a row before an endpoint is turned off |
| `WEBHOOK_TIMEOUT_SECONDS` | 10 | Per request |
| `WEBHOOK_SECRET_OVERLAP_HOURS` | 24 | The old secret keeps signing after **New secret** |
| `EXPORT_XLSX_MAX_ROWS` | 200000 | Rows in one Excel export (CSV has no limit) |

## Controls for finance teams

| Control | How it works |
| --- | --- |
| Roles | Viewer, Reviewer, Approver, Admin per organization (Team page). Every view and API call checks the role. |
| Platform admins | Superusers have no access to an organization's data unless they are a member, with that member's role and approval limit. In the platform admin (`/admin/`) they manage organizations, memberships, billing and sign-ups; every client record (documents, shipments, bills, disputes, rates ...) is read only there, an organization's controls (currency, time zone, two-factor, maker-checker, threshold, rates) are fixed once it exists, and organizations can't be deleted. Support access means adding yourself as a member, which the client's audit log shows ("added root as viewer through the platform admin"). |
| Maker-checker | Whoever uploaded, corrected, moved or accepted issues on a shipment can't approve it (Settings, on by default). |
| Approval limits | Per person, in the home currency. Foreign invoices are converted with the rates in Settings; with no rate the check fails closed. |
| Error overrides | Only approvers can override an error, and only with a written reason that is kept in the audit log. Rejections need a reason. |
| Locking | Approved and posted shipments are read only. Reopening is recorded. |
| Audit log | Every sign-in, edit, decision and setting change with person, time, IP and request ID. Filter and export CSV. Not editable. |
| Sign-in | Own sign-in page (the admin site uses it too), lockout after repeated failures, idle timeout, optional or required two-factor (authenticator app + recovery codes). |
| Headers | Content Security Policy without inline scripts, HSTS and secure cookies in production, request IDs on every response. |
| API | Organization API keys (read only, or read and upload) with scopes, stored hashed, with expiry, revocation and a per-minute rate limit. |
| Operations | `/health/` (alive) and `/health/ready/` (database, cache, storage), JSON logs with `LOG_FORMAT=json`, CI in `.github/workflows/ci.yml`. |

## Disputes

When a check finds money at risk on an invoice (an overcharge, a duplicate, charges far above the
vendor's usual rate), ShipMatch helps get it back.

1. On the shipment, **Disputes with vendors** lists each invoice with problems worth raising. Issues
   with money at risk are ticked already; checks about your own paperwork (missing B/L, low confidence)
   are never offered. **Dispute with vendor** opens a draft.
2. The draft is written from the evidence: invoice number, date and total, B/L, containers, each problem
   in plain words with its amount, the vendor's quote reference when a check recorded one, and a request
   for a credit note or a corrected invoice. With AI reading switched on, the wording can be polished by
   AI; the polished text is used only if it still contains every amount, invoice number, container and
   reference, otherwise the standard wording is kept. Everything stays editable.
3. An approver sends it. The email goes through the normal email settings (`EMAIL_HOST` ...), with the
   invoice PDF attached, replies directed to the accounts payable mailbox (Settings > Disputes), an
   optional copy to that mailbox, and the Message-ID kept on the dispute. The vendor's address is
   remembered for next time.
4. **Disputes** in the sidebar lists them by status and vendor, with age, money waiting on vendors and
   money recovered this month and quarter. Each dispute has a timeline: log the vendor's reply, add notes,
   move the follow-up date, record the credit (amount, optionally the credit note document), mark it
   resolved, or close it without recovery with a reason.
5. A dispute still waiting after its follow-up date is flagged once (hourly check, in the organization's
   time zone), shown in the sidebar count and sent as an alert.
6. When a credit note arrives (by email or upload) from the same vendor and names the disputed invoice,
   the dispute's timeline says so and its credit form is filled in with that document and amount. An
   approver checks it and saves: a document from outside never records a credit or releases a shipment
   on its own. Recovered credits appear under "Recovered after payment" on the Savings page.

| Who | Can |
| --- | --- |
| Viewer | See disputes |
| Reviewer | Draft and edit disputes, log replies, add notes, change follow-up dates |
| Approver, Admin | Also send disputes, record credits, resolve, close, approve without waiting |
| Admin | Dispute email settings (reply-to address, signature, follow-up days) |

**How a dispute affects its shipment**

* A draft has no effect.
* While a sent dispute waits for the vendor (sent or acknowledged), the shipment can't be approved and the
  disputed issues stay open. The approval panel says why.
* An approver can **approve without waiting**, with a note: the disputed issues are resolved with that note,
  the bill posts as invoiced, and the dispute stays open to collect the credit.
* Recording the credit, or closing the dispute with a reason, resolves the disputed issues that are still
  open, in the name of the person who did it (so maker-checker counts them as a preparer, as with an override).
* Approved and posted shipments are read only: a dispute raised after approval tracks the money but never
  changes the shipment.
* Checking a shipment again re-creates its open issues; disputes re-attach to them automatically, and keep a
  copy of the evidence as it was sent.

`apps.disputes.savings.recovered(org, start, end)` returns the amount recovered in the home currency
(converted at the rate set when each credit was recorded), for the savings dashboard.

## Alerts

Settings > Alerts (admins) sends the team a message when something needs them:

| Alert | When |
| --- | --- |
| Shipment needs review | A shipment's status changes to needs review |
| Shipment ready for approval | Every check passed or was accepted |
| Bill posting failed | QuickBooks or Xero refused a bill (`bill.failed`), with the reason |
| QuickBooks needs reconnecting | Intuit stopped accepting the connection (`qbo.needs_reconnect`) |
| Xero needs reconnecting | Xero stopped accepting the connection (`xero.needs_reconnect`) |
| Bill voided or deleted in accounting | A posted bill or credit was voided or deleted in QuickBooks or Xero (`bill.voided_in_accounting`) |
| Dispute overdue | A vendor didn't answer by the follow-up date |
| Credit received | A credit was recorded on a dispute |
| Daily summary | Once a day at the chosen local hour: to review, to approve, failed postings, overdue disputes, money at risk. Skipped when there is nothing to report |

Channels: **Slack** (incoming webhook), **Microsoft Teams** (a Power Automate workflow "When a Teams webhook
request is received", which posts an Adaptive Card) and **email** (HTML with a plain-text part). Each channel
picks its alerts; **Send test message** checks it at once. Webhook addresses are encrypted at rest
(`FIELD_ENCRYPTION_KEY`), never shown in full and never written to the audit log. Only https addresses on
`hooks.slack.com`, `*.webhook.office.com`, `*.logic.azure.com` and `*.environment.api.powerplatform.com` are
accepted (no other hosts, ports, user names or redirects), checked when saved and before every request.

Alerts are queued after the action commits and sent by Celery. HTTP 408/425/429/5xx, timeouts and mail
server errors are retried with backoff (30 s, 1, 2, 4 min; `Retry-After` respected) up to 5 attempts; other
answers fail at once. The last 30 messages, with the service's answer, are listed on the Alerts page. An
alert that fails never stops the work that triggered it. Links in alerts use `SITE_URL`.

Celery beat runs `flag_overdue_disputes` and `send_daily_digests` hourly and `retry_stalled_deliveries`
every 10 minutes (`CELERY_BEAT_SCHEDULE`); the Docker stack runs beat, elsewhere start `celery -A config beat`. Without workers
(`CELERY_TASK_ALWAYS_EAGER=1`), alerts are sent inline: rate limits and 5xx answers are retried at once,
timeouts are left for the 10-minute sweep, and the scheduled checks need beat.

## Team workflow and firm view

Built for a team that reviews and approves every day, and for bookkeeping firms that work for many clients
(`apps/workflow`).

**Bulk actions.** In the review queue, tick shipments (the header box selects the page, Shift-click selects a
range) and **Approve**, **Assign** or **Post to QuickBooks**. Approving and posting first show a confirmation
page that lists, per shipment, what will happen and why any would be skipped; nothing changes until you
confirm. Each shipment goes through exactly the single-approval path: role, approval limit (per shipment),
maker-checker, open errors, disputes waiting for the vendor and the organization's two-factor rule. Posting
takes approved shipments only. Every shipment gets one audit row (`shipment.approved`, `shipment.assigned`,
`shipment.post_requested`, or `shipment.bulk_skipped` with the reasons), each with the same `batch_id`; the
results page reads the batch back from the audit log.

**Keyboard shortcuts.** `j`/`k` move through rows (on a shipment: previous and next shipment in its tab),
`Enter` opens, `x` ticks the row, `a` approves (always opens a confirmation first), `r` opens the reject box,
`e` edits the selected value, `g` then `q`/`d`/`s` goes to the queue, documents or savings, `/` searches and
`?` shows the list. They never fire while typing in a field or with Ctrl, Alt or Cmd held, announce row moves
to screen readers, and each person can turn them off (the keyboard button in the top bar, or
`/account/shortcuts/`, which also holds the person's email preferences).

**Assignment.** Each shipment can be assigned to a reviewer, approver or admin of its organization (shipment
page strip, or in bulk). The queue has **Everyone / Assigned to me / Unassigned** filters and the sidebar
counts the open shipments assigned to you. **Settings > Assignment** (admins) shares out new work when a
shipment first needs review or is ready to approve: take turns among reviewers (optionally approvers too),
or by vendor (a rule per vendor name, falling back to taking turns or to nobody). A shipment someone
unassigned on purpose is left alone. Every change is audited with the previous assignee; the assignee gets
a notification and an email, and channels subscribed to the **Assigned to you** alert get a message.

**Comments and mentions.** Shipments and documents have threaded comments (one level of replies). Type `@`
to pick a member of the organization (the autocomplete and the mention parser only know members of that
organization, so nobody outside it is ever notified). Mentioned people and the author of the comment being
answered get a notification and an email; channels can subscribe to **Mentioned in a comment** (off by
default). Authors can edit or delete their own comment for `COMMENT_EDIT_MINUTES` (15); edits and deletes are
audited with the previous text. Viewers read comments but can't write them. The bell in the top bar shows
unread notifications from every organization you still belong to; **Notifications** lists them all.

**Approve from email or Slack.** "Ready for approval" alerts link to `/approve/<token>/`, a focused,
phone-friendly page with the totals, your approval limit, open issues, the documents (PDF links) and
Approve/Reject. The token is signed (Django signing, its own salt) and expires after
`APPROVAL_LINK_MAX_AGE_HOURS` (72); it only says which shipment to show, with "You were sent here to approve
SHP-x". It never signs anyone in: the normal sign-in (and two-factor when it is on) comes first, the person
must be a member of the shipment's organization (otherwise 404), opening the page changes nothing (GET only;
the buttons are POST forms with CSRF), and an altered or foreign token is refused. An expired link still
opens the shipment for members, without the banner. The same page is at `/work/shipments/<id>/approval/`
from **My work**, and after a decision it offers the next shipment waiting for your approval.

**Firm view.** People who belong to more than one organization get **All clients** (`/clients/`) and **My
work across clients** (`/my-work/`) at the top of the sidebar. All clients shows, per organization: needs
review, ready to approve, assigned to you, failed postings, QuickBooks connection health, money at risk on
open issues (in the home currency) and the oldest waiting shipment; every column sorts, and one click
switches into the client with the normal organization switch. My work lists shipments assigned to you or
ready for your approval (checked with the approval rules, so ones you prepared or that exceed your limit are
left out) across all your organizations, with the client's name. Scoping is by membership only: platform
superusers see other organizations in the platform admin, never here. Clients that require two-factor are
listed without numbers until you turn it on. With `FIRM_CAN_CREATE_ORGS=1`, admins of an organization can
create a new client organization from All clients (they become its admin; controls can be copied from an
organization they administer). Turned off for the shared demo accounts.

| Setting | Default | Meaning |
| --- | --- | --- |
| `APPROVAL_LINK_MAX_AGE_HOURS` | 72 | How long an alert's approval link names its shipment |
| `COMMENT_EDIT_MINUTES` | 15 | How long authors may edit or delete their comment |
| `BULK_MAX_SHIPMENTS` | 100 | Most shipments in one bulk action |
| `FIRM_CAN_CREATE_ORGS` | 0 | Let organization admins create client organizations |

Personal emails (mentions, replies, assignments) use the normal email settings and are never sent from a
public demo. Slack and Teams incoming webhooks post to a channel, not to a person, so personal alerts there
name the person instead.

## Switching on the real providers (.env)

| Capability | Setting |
| --- | --- |
| AI reading | `EXTRACTION_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` (default model `claude-sonnet-5-5`; `LLM_MODEL=claude-haiku-4-5` is cheaper). `LLM_INPUT=pdf` also sends the PDF itself for layout-heavy invoices. Or `openai` + `OPENAI_API_KEY`. |
| Scanned PDFs | Read by Claude automatically when Claude is the reader (`OCR_PROVIDER=auto`). Alternative: `OCR_PROVIDER=textract` + AWS keys. |
| Gmail | OAuth client JSON at `GMAIL_CREDENTIALS_FILE`, then `python manage.py gmail_auth --org demo` |
| Email intake | `INBOUND_EMAIL_DOMAIN` + Postmark or Mailgun, `MS_CLIENT_ID`/`MS_CLIENT_SECRET` for Microsoft 365, IMAP in the app (see Email intake) |
| QuickBooks | `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_ENVIRONMENT=sandbox`, then Settings > Accounting > Connect QuickBooks |
| Xero | `XERO_CLIENT_ID`, `XERO_CLIENT_SECRET` (empty for a PKCE app), `XERO_REDIRECT_URI`, then Settings > Accounting > Connect Xero (see Xero) |
| Email (invites, resets, disputes, alert emails) | `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`. Without it, emails print in the server window and the Team page shows the link to copy. |
| Links in alerts | `SITE_URL`, the public address of ShipMatch, e.g. `https://ap.example.com` |
| Card payments | `BILLING_ENABLED=1`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_*` (see Billing) |
| Several web processes | `CACHE_URL=redis://...` so lockouts and rate limits are shared |

## Evidence highlighting

On a shipment or document page, click any extracted value (or press its target button, or tab into
its field) and the PDF on the right scrolls to the page and highlights exactly where that value was
read. Hovering a value previews its box. Line item rows highlight their description and amount.
Issues about a value have a **Show on page** button: a total that doesn't match highlights the total
and every line amount, a container typo or a container missing from the B/L highlights that
container, a possible duplicate highlights the invoice number. Screen readers hear "Highlighted total
amount on page 2".

How it works:

- After extraction, `apps/documents/services/locate.py` finds each value among the PDF's words and
  their positions (pdfplumber) and stores boxes on the field (`ExtractedField.location`, fractions of
  the page, so they fit any zoom). It is plain code, no AI: amounts are compared as numbers
  (1,234.50, 1234.5, 1.234,50, 1 234,50, $1,234.50, USD 1,234.50), dates as dates (2026-03-04,
  03/04/2026, 4 Mar 2026, March 4, 2026, 04-Mar-26), references letter by letter so `MSCU 123456-5`
  matches `MSCU1234565`, and names as runs of words, also when wrapped over two lines.
- When a value is printed more than once, the copy next to a matching label wins ("Invoice No",
  "Total", "B/L"), a total prefers the bottom of the last page over a subtotal, a vendor name prefers
  the letterhead, and two line items with the same amount get their own rows.
- A value that isn't printed (for example one a reviewer typed) gets no box, and the page says so: a
  wrong box would be worse than none. Correcting a value finds the new value on the page.
- Scanned pages: with `OCR_PROVIDER=textract` the word positions Textract returns are kept
  (`OcrLayout`) and used the same way. Scans read without word positions (Claude OCR, or no OCR)
  show the browser's own PDF viewer and "Location not available for scanned pages".
- Locating never stops a document from being processed; errors are logged and skipped.
- Documents read before this feature: `python manage.py locate_fields` (add `--org demo`, document
  ids, or `--all` to redo everything). It is safe to repeat.

The viewer is PDF.js 6.3.289 (Apache 2.0, legacy build for current and older browsers), vendored in
`static/vendor/pdfjs/`, loaded only on review pages, from our own server, within the existing Content
Security Policy. If it can't load, the page keeps the browser's PDF viewer exactly as before. To
update it: `npm pack pdfjs-dist@<version>`, then copy `legacy/build/pdf.min.mjs`,
`legacy/build/pdf.worker.min.mjs`, `LICENSE` and `wasm/{jbig2.wasm,jbig2_nowasm_fallback.js,openjpeg.wasm}`
with their licenses.
## Rates and savings

ShipMatch compares every freight invoice with the rates the vendor agreed to, flags charges above
the quote and extra charges nobody approved, and shows the money caught.

**Rates** (sidebar). Quotes per vendor, lane (port of loading and port of discharge, typed as names
or UN/LOCODEs; an empty port means any), equipment (20GP, 40GP, 40HC, 45HC, 20RF, 40RF, LCL or any)
and validity period, with charge lines per container, shipment, B/L, kg, cbm, day or hour. Approved
extra charges per vendor set free time and caps (for example detention after 4 free days, at most
100.00 a day). CSV import (download the template; the whole file is checked and nothing is saved if
a row has a problem; the problems are listed by row and can be downloaded) and CSV export in the
same format. Every change is in the audit log, and open shipments are checked again right away.

| Permission | Can |
| --- | --- |
| view | See quotes, approved extras, charge names, checking rules and Savings; export CSV |
| approve | Add, change, copy, archive, delete and import quotes and approved extras; teach charge names; check open shipments again. Reviewers prepare shipments, so they can't change the rates their own shipments are checked against |
| manage | Change the checking rules (tolerance and switches) |

**Checks** (registered from `RatesConfig.ready()`, titles in `apps/shipments/labels.py`). The lane
comes from the bill of lading in the same shipment, the equipment from the B/L and invoice text, the
date from the B/L issue date. Invoice lines are named with a keyword table (`apps/rates/charges.py`:
THC, DTHC, OTHC, BAF, LSS, CAF, ISF, D&D, CET, PSS ...), then names the organization taught under
Rates > Charge names, then optionally the AI (`RATE_AI_CLASSIFY`, remembered per name), else "other".

| Check | Severity | Amount at risk |
| --- | --- | --- |
| Charged more than the quote (`over_quote`) | Error | The excess, per charge code, when it is more than the tolerance (larger of 2% and 10.00 by default). A base charge the quote doesn't list counts as quoted at zero, unless the quote is all-in |
| Extra charge not approved (`unapproved_accessorial`) | Warning | The whole line if neither the quote nor the approved list covers it; the excess if it is above the cap |
| Extra charge couldn't be checked (`accessorial_unchecked`) | Warning | None: approved, but the line doesn't show the days or hours |
| No matching quote (`no_quote`) | Warning | None: the vendor has quotes, but none fits the lane, date and equipment (can be turned off) |
| Quote in a different currency (`quote_currency`) | Warning | None: no exchange rate to compare with |

Vendors with no quotes or approved extras on file are not checked unless "Check every vendor's extra
charges" is on.

**Savings** (sidebar, view permission) adds up `ValidationIssue.amount_at_risk` from every check
(including duplicates and totals that don't add up) through a ledger that survives re-validation
(`apps/rates/ledger.py`). `apps/rates/savings.py` has `summary(org, start, end)` and the outcome
rules: prevented (shipment rejected, or the invoice corrected or removed; a partial correction counts
the reduction), still at risk (open issue), accepted (warning accepted, error overridden, or approved
with the warning open). Catches that disappear because a quote or rule changed are shown as withdrawn
and not counted; money flagged by two checks on one invoice counts once. A disputes app can register
`savings.register_recovery_source(fn)`; `fn(org, start, end)` returns money recovered after payment.
The dashboard shows "Overcharges caught this month".

**ROI calculator** at `/roi/` (no sign-in, for sales calls; linked from Savings): invoices per month,
minutes per invoice today and with ShipMatch, loaded hourly cost, share of invoices with an
overcharge, average overcharge and monthly price give hours saved, labor saved, overcharges caught,
net annual benefit and payback. It works without JavaScript; `static/js/roi.js` updates it live with
the same formulas as `apps/rates/roi.py`. The prefilled values are examples, not industry figures.

**Demo**:

```bash
python manage.py generate_dataset --out datasets/synthetic --shipments 20 --seed 42 --accessorials
python manage.py ingest_folder datasets/synthetic --org demo
python manage.py seed_rates --org demo      # quotes and approved extras for the synthetic vendors
```

`--accessorials` adds demurrage, detention, waiting time, exam fees and other extras to some freight
invoices; without it the dataset is unchanged. `seed_rates` is idempotent and re-checks open
shipments, so the review queue, Savings and the dashboard show real over-quote and extra-charge
catches (plus one "no matching quote" lane). Settings: `RATE_TOLERANCE_PERCENT`,
`RATE_TOLERANCE_AMOUNT` (defaults for new organizations), `RATE_AI_CLASSIFY`.

## Landed cost and shared invoices

Importers need the true cost of each product, and forwarders often send one invoice for several
shipments. Both live in `apps/landed`.

**Landed cost** (a section at the bottom of every shipment, and **Landed cost** in the sidebar). The
products are the lines of the shipment's commercial invoices: description, quantity, unit price, value,
and when printed SKU, HS code, weight (kg) and volume (cbm) (optional fields on the commercial invoice
schema; the rule reader takes them from a detail line under each product, the AI reader from the page).
Every charge is converted to the home currency with the organization's exchange rates and spread over the
products in whole cents (largest remainder), so the parts always add up to the charge exactly. Each product
shows its landed cost, cost per unit and uplift on goods value; CSV and Excel exports list the products and,
in Excel, every charge and how it was spread.

| How a charge is spread | |
| --- | --- |
| By value (default), quantity, weight or volume | Settings > Landed cost (admins), per type of charge if wanted (freight by volume, duties by value) |
| For one shipment | "Change how charges are spread on this shipment" (reviewers), until it is approved |
| Order | The setting for the charge's type, else the basis the charge's source suggests (duties: value), else the method chosen |
| Fallbacks | Weights or volumes missing on some products: spread by value instead (then quantity, then equally), with a note on the page |

Charges come from charge sources: freight invoices, credit notes (which reduce cost) and this shipment's
share of shared invoices are built in. A possible duplicate invoice is left out until someone decides. Other
apps add sources from `AppConfig.ready()` with
`apps.landed.services.charges.register_charge_source(fn)`; `fn(shipment)` returns charges as
`{"code", "amount", "currency", "basis" (optional hint), "description", "source", "category"}` (a customs
entry adds duty this way; codes such as `customs_duty`, `vat` or `mpf` count as duties and taxes). A failing
source is skipped with a note. Commercial invoice lines without a quantity that read like a charge
("Freight", "Insurance") or a discount are spread as charges, not counted as products. Without a commercial
invoice, product lines or an exchange rate, the page says what is missing.

When a shipment is approved its landed cost is frozen with that day's exchange rates (`LandedCostRun`), so
**Landed cost by product** (view permission) doesn't move later: per product (supplier and SKU, or the
description when there is no SKU) it shows shipments, quantity, average and last cost per unit, the change
from the previous shipment, a trend line and the uplift, for a period, with an option to include shipments
still in review (worked out live). Exports: CSV (one row per product per shipment) and Excel (summary and
detail sheets). Text cells can't become formulas (CSV through `apps.core.csvsafe`, Excel stored as text).

**Shared invoices.** A freight invoice that names another open shipment by B/L number, container or PO (in
its fields or anywhere in its text) is split between the shipments. References the invoice's own shipment
also has (a PO split over two shipments, a reused container) don't count. The invoice stays matched to one
shipment (the one shown with "the invoice is here"); each shipment carries its share (`InvoiceAllocation`),
and the shares always add up to the invoice total:

* lines that name one shipment's B/L, container or PO go to it whole; the other lines are split by
  containers (or equally);
* or, chosen by a reviewer: by containers, by weight or volume on each shipment's commercial invoice,
  equally, or typed amounts that must add up to the cent (the page shows what is left to assign).

Each shipment's page shows "Shared invoice: your share 1,240.00 of 3,100.00" with the other shipments,
what the invoice names for each, and their status. A detected split is a suggestion: a reviewer confirms it
(or saves their own split, which confirms it), recorded in the audit log on every shipment. Confirming also
closes the invoice's "container not on the bill of lading" errors for containers that are on another
shipment's B/L in the split. **Not a shared invoice** (with a reason) keeps the whole invoice on its own
shipment; **Back to the automatic split** undoes a typed split. Splits are locked once any shipment in them
is approved. A B/L that arrives after the invoice gets its share then; an invoice that names an approved
shipment says so instead of changing it, and gives it a share if it is reopened.

| Check or rule | Effect |
| --- | --- |
| Shared invoice: check the split (warning) | On every shipment in the split until a person confirms that exact split |
| Shared invoice split doesn't add up (error) | On every shipment in the split, e.g. after the invoice total was corrected |
| Invoice names an approved shipment (warning) | On the invoice's shipment |
| Approval | No shipment in a split is approved before the split is confirmed; the invoice's own shipment is approved last, after every other shipment with a share. Approval limits count the shares a shipment carries (the invoice's own shipment counts the whole invoice) |
| Posting | Blocked until every shipment with a share is approved (`register_posting_blocker` in `apps/shipments/services/approval.py`, checked by **Post bills to QuickBooks**) |

For posting one bill split per shipment, `apps.landed.services.allocation.bill_lines(doc)` returns
`[{"description", "amount", "shipment_reference"}]` adding up to the invoice total, or `None` for an invoice
that isn't shared. `posting_blockers(shipment)` in the same module gives the reasons not to post yet.

| Who | Can |
| --- | --- |
| Viewer | See landed cost, shared invoices and the report; export |
| Reviewer | Change how one shipment's charges are spread; split, confirm, un-share or reset a shared invoice |
| Admin | Settings > Landed cost |

| Setting | Default | Meaning |
| --- | --- | --- |
| `LANDED_DEFAULT_METHOD` | `value` | How charges are spread for organizations that haven't chosen (`value`, `quantity`, `weight`, `volume`) |
| `SHARED_INVOICE_DETECT` | 1 | Suggest a split when a freight invoice names other shipments; 0 = only splits reviewers start |

Demo: `python manage.py seed_landed --org demo --rounds 3` imports three rounds of three shipments from one
supplier (commercial invoices with SKUs, HS codes, weights and volumes, and B/Ls) plus one forwarder invoice
covering each round (`--unattributed` for an invoice whose lines don't name a B/L). The helpers are in
`synthetic/landed.py`; the default dataset doesn't change.

Known limits: matching itself is unchanged, so a forwarder invoice that arrives before any of its B/Ls can
pull a later B/L into its shipment by container, and a shipment known only by PO is merged into the shipment
of an invoice that lists its PO (send B/Ls first, or move the document). Spreadsheet invoices don't read
weight or volume columns yet. The payable total at the top of a shipment still shows whole invoices.
## Customs entries and free time

Two more document types are read, matched and checked like the others: **customs entries** (US entry summaries,
CBP Form 7501, and other countries' import declarations) and **arrival notices** (carrier or forwarder arrival
notices and delivery orders). Neither is a bill: duty and fees go to landed cost, arrival notices feed free time.

**Reading.** The classifier knows both types by title ("Entry summary", "Customs import declaration", "Arrival
notice", "Delivery order") and by words only the form itself prints; a broker's invoice that bills duty, MPF and
HMF stays a freight invoice. Without AI, the rule readers (`apps/customs/services/readers.py`) read the numbered
7501 boxes even when several share a line, the tariff lines (line, HTS number, description, entered value, rate,
duty; a Chapter 99 line such as 9903.88.15 without its own value), the fee summary (499 MPF, 501 HMF, other class
codes and box 38 tax as other fees) and the totals; on arrival notices the ETA, discharge date, free days, how they
are counted, last free days per container and the charges due before release. With AI on, the same fields are asked
for (every field nullable, the tariff lines as a table). Tables are located on the PDF row by row, so **Show on
page** highlights a tariff line and its duty.

**Matching.** By B/L, then container, as usual. A customs entry that shares neither with an open shipment joins
the shipment that already has the same entry number (a corrected copy) or a document printing it; a broker invoice
without a B/L that prints a US entry number joins the shipment of that entry.

**Checks** (registered from `CustomsConfig.ready()`, titles in `apps/shipments/labels.py`). Amounts are in the
entry's currency (USD for a 7501); differences up to `CUSTOMS_DUTY_TOLERANCE` (1.00) are rounding.

| Check | Severity | Amount at risk |
| --- | --- | --- |
| Duty on a line doesn't match its rate (`duty_line_mismatch`) | Error when overstated, warning when understated | The overstatement. Rate x entered value per line; a Chapter 99 line uses the value of the line above; specific and compound rates (2.4¢/kg) are not recalculated |
| Total duty doesn't match the lines (`duty_total_mismatch`), entry total doesn't match duty plus fees (`duty_fees_total_mismatch`), entered values don't add up (`entered_value_total_mismatch`) | Error when overstated, else warning | The overstatement |
| Merchandise processing fee is wrong (`mpf_incorrect`) | Error above the maximum or overstated; warning below the minimum or understated | Stated minus correct MPF |
| Harbor maintenance fee is wrong (`hmf_incorrect`) | Error when overstated | The overstatement (0.125% of the entered value) |
| Tariff number looks wrong (`hts_code_format`) | Error | None. US: 10 digits (8 for Chapter 99); other countries: 6 to 10 |
| Possible duplicate customs entry (`duplicate_customs_entry`) / received again with other amounts (`customs_entry_changed`) | Error / warning | The entry's total duty and fees (counted once on Savings) |
| Entered value below / above the commercial invoices (`entered_value_low` / `entered_value_high`) | Error / warning | Above: the extra duty, HMF and MPF paid on the difference. The invoices are converted with the entry's exchange rate (else the organization's rate; with neither, nothing is compared); `CUSTOMS_VALUE_TOLERANCE_PERCENT` (1%) either way |
| Country of origin differs from the invoice (`origin_mismatch`) | Warning | None (origin printed on the commercial invoice: "Country of origin", "Made in") |
| Tariff number may not fit the goods (`hts_description_doubt`) | Warning | None. Optional AI check (`CUSTOMS_AI_HTS_CHECK`), asked once per number and description after the entry is read; no AI or a failed call means no check |
| Free time dates don't fit (`free_time_dates`) | Warning | None: a last free day before discharge |

**MPF is a dated table**, `apps/customs/fees.py`: 0.3464% of the entered value between a minimum and maximum that
CBP changes every 1 October (FAST Act inflation adjustment), from 2018 to fiscal year 2027 ($34.58 to $670.86 from
1 October 2026, 91 FR 46530), each row with its source. The entry date picks the row. Add the next year's row in
code, or meanwhile set `CUSTOMS_MPF_TABLE` (JSON rows `{"from", "min", "max"}`); a broken value is reported by
`manage.py check`. HMF is 0.125% (`CUSTOMS_HMF_RATE`). Settings > Customs shows the rows in force.

**Landed cost.** `apps/customs/services/charges.py: duty_charges(shipment)` returns
`[{code, amount, currency, basis_hint, label, entry_number, document_id}]` for duty, MPF, HMF and other fees as
stated on the shipment's entries (the latest copy of each entry number only). `basis_hint` is `value` (spread by
goods value) or `shipment`. The landed cost feature registers it with `register_charge_source`.

**Free time.** Each container of an arrival notice gets a row (`ContainerFreeTime`) with ETA, discharge date, the
last free day for demurrage (pick up by) and for detention (return the empty by), updated whenever its shipment is
checked; the latest notice wins for each date it prints, and a printed last free day always beats a computed one.

* Demurrage days count from the day after discharge (or actual arrival; with only an ETA the date is marked as
  estimated). Detention days count from the day after pickup, unless the notice says "from discharge".
* Saturdays, Sundays and the organization's holidays are **not counted unless the notice says calendar days**
  ("including weekends"). Settings > Customs can make calendar days the default for notices that don't say. The rule
  used is shown under the free time table, for example "working days (the notice doesn't say, so weekends and
  holidays are not counted)".
* "Today" is the organization's date in its time zone, for countdowns, alerts and date checks.

The shipment page has **Customs duty** (each entry's entered value against the invoices, duty, MPF with the limits
of its date, HMF, totals; green when they agree) and **Free time** (countdown per container, the estimated daily
cost from Rates). Reviewers enter the **picked up** and **returned empty** dates there (never in the future, return
after pickup, pickup after discharge; viewers see them; every change is in the audit log). A return closes the
container. The dashboard shows **Containers near last free day**; **All containers** lists them with filters.

**Alerts** (Settings > Alerts, "Last free day coming up" and "Free time passed"): Celery beat runs
`apps.customs.tasks.check_free_time` hourly; each organization is handled in its own time zone from
`CUSTOMS_ALERT_HOUR` (7). "Last free day in 2 days" goes out once per container per last free day when it is within
the lead days (`CUSTOMS_LFD_ALERT_DAYS`, per organization in Settings > Customs); "Free time passed: demurrage is
accruing" once the day after, with the estimated daily cost from the approved demurrage, storage, detention or per
diem rate of the carrier or of the shipment's freight invoice vendors in Rates (or a per-day quote line). Without a
rate the message says so. An updated notice with a new date alerts again.

**Demo data.** `generate_dataset --customs` adds 7501-style entries (some with a planted duty, MPF, total, HMF or
tariff number error) and arrival notices dated around today to the synthetic set; without it the dataset is
unchanged. `synthetic/customs.py` has the helpers (`cbp7501`, `customs_declaration`, `arrival_notice`,
`commercial_invoice` with a printed origin). They are marked as synthetic samples.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CUSTOMS_LFD_ALERT_DAYS` | 2 | Lead days for last free day alerts (organization default) |
| `CUSTOMS_ALERT_HOUR` | 7 | Local hour from which the day's alerts are sent |
| `CUSTOMS_DUTY_TOLERANCE` | 1.00 | Rounding allowed on duty, fees and totals |
| `CUSTOMS_VALUE_TOLERANCE_PERCENT` | 1.0 | Entered value vs commercial invoices |
| `CUSTOMS_MPF_TABLE` | empty | Extra or replacement MPF rows (JSON) |
| `CUSTOMS_HMF_RATE` | 0.125 | Harbor Maintenance Fee, percent |
| `CUSTOMS_AI_HTS_CHECK` | 1 | AI check of tariff numbers against descriptions (only with AI reading) |
## Month-end close

**Month-end** in the sidebar (under Controls) answers two questions at the end of a period: what do we owe
for freight that isn't in the books yet, and does each vendor's statement agree with us.

**Accruals.** Choose a period end (the last day of last month by default). The report has two kinds of
lines, each with its method, the basis in words and a confidence label:

| Lines | Which | Amount |
| --- | --- | --- |
| Received, not booked | Invoices and credit notes ShipMatch has that belong to the period (dated on or before the period end, or for a shipment that shipped by then) and aren't posted to QuickBooks with a bill date in the period: still in review, ready, approved, failed, or posted later | The document's own amount (credit notes negative). Supplier (goods) invoices too, unless turned off |
| Not yet invoiced | Shipments that shipped on or before the period end (on-board date printed on the B/L, else the B/L issue date) within the look-back window, for each charge group no freight invoice bills yet: ocean freight and surcharges, destination port and customs, delivery (trucking). "Partly invoiced" shows which groups are billed | An estimate, tried in this order: the quote on file from the likely vendor that fits the lane, date and equipment (per-container charges times the containers, per-shipment and per-B/L charges once); the vendor's median per container on the same lane and equipment; the organization's median per container. With no basis the line has no amount and is flagged |

Confidence: a quote that fits with nothing assumed is high (0.9); each assumption lowers it (lane or equipment
not printed, container count unknown, and most a vendor that isn't billing the shipment yet); a vendor median
is medium, an organization median low. "Needs a look" lists lines with no amount or low confidence. Every
estimated line can be adjusted by an approver: **Use a known amount** (with a note) or **Don't accrue it** (for
example, the customer collects at the port); both are audited and apply until the real invoice arrives.

Not counted and listed with the reason: rejected shipments, open possible duplicate invoices, shipments with no
B/L date, shipments older than the look-back window, charges marked as not needed. Amounts in a currency with no
exchange rate (Settings) are listed but left out of the totals, with a warning.

Totals by vendor and by account. Each line is debited to the vendor's account (Settings > QuickBooks, or set on
an invoice), else the freight or goods account in Month-end settings, and the total is credited to the accrued
liabilities account. **CSV** has every line (formula-safe). **Journal entry (Excel)** has the entry per account
and vendor, the reversing entry dated the first day of the next period, every line, and notes (version, who
locked it, the report's SHA-256 fingerprint, warnings), with the header in row 1 so the sheets can be imported.

**Locking.** An approver locks a period after its last day: the report is stored exactly as shown (every line,
total and the settings used), audited, and never changes; the page then shows the locked version and exports come
from it. The live report keeps using what ShipMatch knows today (an invoice that arrived after the period end
replaces its estimate), and shows how it differs from the locked version. Locking again needs a reason and
creates the next version; earlier versions stay readable. A lock based on a stale page is refused.

**Vendor statements.** Upload the statement a vendor sends (PDF, XLSX or CSV). With AI reading on, the AI reads it
into the statement schema (`apps/close/schemas.py`: vendor, statement date, currency, opening and closing
balance, lines of type invoice, credit, payment or opening balance with number, date, reference/B/L, amount and
balance); any amount it returns that isn't printed in the statement is dropped. Without AI, or when the AI fails,
rules read it: the intake spreadsheet reader for XLSX and CSV (header row, then each row; statement date, vendor,
currency and balances from the label cells), and the text lines of a PDF (each line with a date and an amount).
Statements are kept in their own model, never as documents: they are never classified, matched to a shipment,
checked or posted. A statement that arrives through normal intake anyway gets an error ("Looks like a vendor
statement"), so it can't be approved and paid as an invoice.

Each statement line is matched with ShipMatch's documents for the same vendor (invoice number compared without
case, spaces and punctuation, then by its digits, then by B/L and amount), and with credits and payments. Findings:

| Finding | Meaning | Effect on the difference |
| --- | --- | --- |
| Matched | Same number and amount (or the vendor applied a credit note naming that invoice) | None |
| Amount differs | Same invoice, different amount; also an invoice whose shipment you rejected | Statement minus ShipMatch |
| On the statement, never received | ShipMatch has no such invoice or credit note. **Request a copy** opens a ready email to the vendor's billing contact (from disputes, else the address their invoices came from); ShipMatch sends nothing itself | The line |
| Received, not on the statement | ShipMatch has it, the vendor doesn't list it | Minus the invoice |
| Credit notes not applied | Credit notes (and dispute credits) the vendor's balance doesn't include | Plus the credit |
| Possible duplicates on the statement | The same number twice, or the same amount, date and reference under another number | The extra line |
| Payments the vendor hasn't applied | Payments you made up to the statement date that the statement doesn't show (recent ones may be in transit) | Plus the payment |
| Payments ShipMatch has no record of | A payment line with no matching payment here | The line |
| Paid and closed | On a statement without payments or an opening balance (open items): invoices paid in full by known payments | Nets to zero |

The summary shows the vendor's balance (as printed, else the sum of the lines), ShipMatch's balance (invoices less
credits and payments up to the statement date) and the difference, explained line by line; the explanations add up
to the difference exactly ("Not explained" shows 0.00). A statement that starts from a balance brought forward is
compared as one opening-balance line, and a printed balance that isn't the sum of the lines is shown too. An
approver marks findings resolved with a note (audited); resolutions survive **Match again** while the finding is
still there. Payments come from **Record a payment** (with the invoices it paid) or **Read payments from
QuickBooks** (BillPayment, linked to bills ShipMatch posted; read only, never on a public demo).

| Permission | Can |
| --- | --- |
| audit (approvers, admins) | See Month-end: accruals, locked versions, statements and findings; download every export. Month-end figures are finance controls, like the audit log, so reviewers and viewers don't see them |
| approve | Lock a period, adjust accrual lines, upload, correct, match again and delete statements, resolve findings, record payments, read payments from QuickBooks |
| manage | Month-end settings (Settings > Month-end): accounts, which charge groups every shipment should carry ("when most similar shipments have it" means at least half of past shipments to the same port of discharge), look-back window, past invoices needed for a median |

| Setting | Default | Meaning |
| --- | --- | --- |
| `CLOSE_ACCRUED_LIABILITIES_ACCOUNT` | Accrued liabilities | Account credited, for organizations that haven't saved their own |
| `CLOSE_FREIGHT_ACCOUNT` / `CLOSE_GOODS_ACCOUNT` | Freight expense / Inventory | Accounts debited when a vendor has none |
| `CLOSE_LOOKBACK_DAYS` | 180 | Older shipments are listed, not accrued |
| `CLOSE_MIN_HISTORY` | 3 | Past invoices needed before a median is used |
| `CLOSE_STATEMENT_MAX_LINES` | 2000 | Lines read from one statement |
| `CLOSE_PAYMENT_SYNC_DAYS` | 180 | How far back QuickBooks bill payments are read |

**Demo.** After loading the synthetic dataset (and optionally `seed_rates`), `python manage.py close_demo --org
demo` adds two shipments that shipped late last month and aren't fully billed, and a Harborlink statement (PDF,
XLSX and CSV in `datasets/synthetic-month-end/`) with one missing invoice, one amount difference and one credit
note ShipMatch has but the vendor hasn't applied; it imports the credit note and uploads the PDF. The generator
helpers are in `synthetic/month_end.py` and are opt-in: the default dataset doesn't change.

## Layout

| Path | Contents |
| --- | --- |
| `apps/core` | Organizations, roles and permissions, audit log, dashboard, settings, middleware (request IDs, time zones, security headers), `seed_demo`, `generate_dataset`, `run_eval` |
| `apps/accounts` | Sign-in, two-factor, lockout, profiles, team management, API keys |
| `apps/documents` | Documents and fields, Pydantic schemas, OCR, classification, extraction, ingestion, Gmail |
| `apps/intake` | Photos, spreadsheets, ZIP archives, multi-invoice PDF splitting and credit notes (see Supported inputs) |
| `apps/shipments` | Shipments, matching, ISO 6346 containers, validation rules, approval rules, review screens |
| `templates/`, `static/` | App shell, sign-in pages, design system (`static/css/app.css`), one script file (`static/js/app.js`), Barlow fonts (SIL Open Font License) |
| `apps/accounting` | QuickBooks and Xero OAuth and clients behind one provider interface, vendor mappings, posted bills, payment status and aging |
| `apps/rates` | Quotes, approved extra charges, charge names, rate checks, savings ledger and summary, ROI calculator |
| `apps/customs` | Customs entries and arrival notices: rule readers, duty and fee checks, dated MPF table, entry number matching, landed cost charges, free time and last free day alerts |
| `apps/disputes` | Vendor disputes: evidence, email drafting and sending, follow-up, recovered totals (`savings.py`) |
| `apps/notifications` | Alert channels (Slack, Teams, email), delivery with retry, daily summary |
| `apps/mailboxes` | Email intake: forwarding addresses (Postmark, Mailgun), Microsoft 365, IMAP and Gmail mailboxes, received-email log |
| `apps/learning` | Vendor learning: profiles learned from corrections, applied while reading |
| `apps/close` | Month-end close: accruals, estimates, locked versions, journal exports; vendor statements, reconciliation, payments |
| `apps/workflow` | Team workflow: bulk actions, assignment and rules, comments and mentions, notifications, keyboard shortcuts, approval links, firm view across clients |
| `apps/demo` | Public try page, demo mode guard rails, `reset_demo`, `purge_try` |
| `apps/billing` | Self-serve sign-up, getting-started checklist, plans, Stripe Checkout/Portal/webhooks, usage limits |
| `apps/integrations` | CSV/Excel exports, outgoing webhooks (guard, signing, retries), API key scopes |
| `apps/api.py` | JSON API (Django Ninja), docs at `/api/docs` |
| `synthetic/` | Fictional dataset generator with ground truth and planted errors |
| `tests/` | pytest-django suite |
| `scripts/quickstart.ps1` | One-command Windows setup and demo |
| `deploy/` | Caddy HTTPS config, nightly backup script, DigitalOcean guide (`deploy/digitalocean.md`) |
| `scripts/deploy-droplet.sh` | One-server install, update, rollback, backup and restore |

## Management commands

| Command | Purpose |
| --- | --- |
| `seed_demo` | Create the demo organization, admin and demo reviewer/approver |
| `pilot [--folder real_docs]` | Import your own PDFs into a separate pilot organization and summarize results and AI cost |
| `try_extraction <file>` | Show what is read from one PDF, photo or spreadsheet, with grounding, tokens and cost (saves nothing) |
| `accuracy_report --org pilot` | Field accuracy measured from reviewer corrections |
| `qbo_check [--post SHP-...]` | Check the QuickBooks connection and optionally post one approved shipment |
| `xero_check [--post SHP-...] [--payments]` | Check the Xero connection, optionally post one approved shipment or read payment status |
| `generate_dataset` | Synthetic PDFs + `ground_truth.json` + `emails.json` |
| `ingest_folder <path>` | Import PDFs, photos, spreadsheets and ZIPs (uses `emails.json` order when present); `--async` to queue |
| `generate_dataset` | Synthetic PDFs + `ground_truth.json` + `emails.json` (`--accessorials` adds extra charges for rate demos, `--customs` customs entries and arrival notices) |
| `seed_rates --org demo` | Demo quotes and approved extra charges matching the synthetic vendors; re-checks open shipments |
| `ingest_folder <path>` | Import PDFs (uses `emails.json` order when present); `--async` to queue |
| `run_eval` | Process a labelled dataset in a fresh org and print accuracy, grouping, error recall and AI cost (`--provider`, `--model`, `--llm-input` to compare) |
| `gmail_auth` / `poll_gmail` | Authorize and poll a Gmail label |
| `poll_mailboxes [--org S] [--sync]` | Check every connected mailbox now (Microsoft 365, IMAP, Gmail) |
| `reprocess <ids> \| --status S` | Re-run the pipeline; human corrections are kept |
| `locate_fields [ids] [--org S] [--all]` | Find where each value is printed on its PDF, for documents read before evidence highlighting |
| `reset_demo [--force]` | Delete and rebuild the demo organizations (`DEMO_MODE=1`; nightly in Celery beat) |
| `purge_try` | Delete try page uploads older than `TRY_RETENTION_HOURS` (hourly in Celery beat) |
| `close_demo [--org demo]` | Month-end demo data: shipments not fully billed and a vendor statement with planted differences |
| `purge_signups` | Delete unconfirmed sign-ups whose link expired (hourly in Celery beat) |
| `send_webhooks` | Send webhook deliveries whose retry is due (every minute in Celery beat) |
