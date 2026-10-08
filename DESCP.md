# ShipMatch — Portfolio Project Brief

> **For agents writing proposals and job applications.** This file is the single source of truth about
> ShipMatch. Read it instead of the codebase. Section 1 has ready-to-paste pitches, Section 12 maps job
> types to the features worth highlighting, and Section 14 lists what must **not** be claimed.
> Last verified against the code: 2026-10-03.

---

## 0. At a glance

| | |
| --- | --- |
| **What it is** | A multi-tenant B2B SaaS that automates **accounts payable for import shipments**. It reads messy shipping paperwork from email, groups each document into its shipment, catches overcharges and errors, routes them to human approval, and posts bills to **QuickBooks Online or Xero**. |
| **Who it's for** | Importers, freight forwarders, customs brokers, logistics companies, and the bookkeeping/accounting firms that serve them. |
| **Core idea** | AI is used **only to read documents**. Matching, validation and money math are deterministic, testable code. A human approves anything uncertain. |
| **Size** | **~66,000 hand-written lines of code**: ~42,400 lines of application Python across 18 Django apps, ~12,900 lines of tests (**844 automated tests, all passing**), ~9,600 lines of templates/CSS/JS, ~800 lines of deploy/ops scripts. That excludes auto-generated migrations and vendored libraries. Also 213 URL routes, 154 templates and a 1,200-line operator README. This is the scale of a funded startup's product, not a weekend demo. |
| **Benchmark** | On the bundled 68-document labelled benchmark: **100% field accuracy, 100% document grouping, 100% planted-error detection, 0 false alarms, $0.00 AI cost** (offline rule reader). Measured 2026-10-03. |
| **Status** | Feature-complete, production-ready product: Docker deploy with automatic HTTPS, Stripe billing, self-serve signup, public demo mode, one-command server install with rollback and backups. |

---

## 1. Ready-made pitches

**One line**
> I built ShipMatch, a multi-tenant SaaS that turns the PDFs, photos and spreadsheets in an AP inbox into
> approved, audit-logged bills in QuickBooks or Xero, using LLM document extraction, deterministic matching
> and a maker-checker approval workflow.

**Short (2–3 sentences)**
> ShipMatch automates accounts payable for import logistics. It pulls invoices, bills of lading and customs
> entries from email (Gmail, Microsoft 365, IMAP, forwarding addresses), extracts the data with Claude/OpenAI
> or an offline rule engine, groups each document into the right shipment, and runs 38 automated checks: overcharges
> against quoted rates, duplicate invoices, customs duty math, container check digits. Approved bills post to
> QuickBooks Online or Xero idempotently, and payment status is synced back every hour.

**Medium (for a proposal body)**
> One recent project is ShipMatch, a production-grade Django SaaS for finance teams in import logistics. The
> problem: AP clerks re-type numbers from hundreds of carrier invoices, bills of lading and customs forms each
> month and miss overcharges. ShipMatch ingests documents from any mailbox or upload (PDF, scans, phone photos,
> Excel, ZIPs, multi-invoice batch PDFs), reads them with an LLM whose outputs are *grounded*, so each value must
> be found in the source text or it is flagged, and matches them to shipments by B/L, container or PO. It
> then validates the numbers in plain code and routes exceptions to reviewers. Approvers sign off under
> role-based limits and maker-checker rules, and bills post to QuickBooks or Xero with idempotency keys,
> rotating OAuth tokens and rate-limit handling. Around that core I built rate-card auditing with a savings
> ledger, landed-cost allocation, US customs (CBP 7501) duty and fee checks, vendor dispute letters, month-end
> accrual journals, vendor statement reconciliation, Stripe subscription billing, signed outgoing webhooks, a
> REST API, Slack/Teams alerts, and 2FA. It is about 66,000 lines of hand-written code across 18 Django apps,
> with 844 automated tests and a labelled accuracy benchmark.

---

## 2. The problem it solves (business framing)

- Import shipments generate **5–10 documents each** (commercial invoice, bill of lading, freight invoices,
  customs entry, arrival notice, credit notes) from different vendors, in different formats, arriving out of order.
- AP teams **re-key numbers by hand**, match documents to shipments in spreadsheets, and pay what's invoiced.
  Overcharges, duplicate invoices, unapproved extra charges (demurrage, detention) and duty miscalculations
  slip through.
- Month-end is painful: freight that shipped but isn't invoiced yet must be **accrued**, and vendor statements
  must be **reconciled** line by line.
- ShipMatch replaces that with: **email in → automatic read → automatic grouping → automatic checks → human
  approval → bill in the accounting system → payment status back**, with every step in an audit log.

**Value levers to cite:** hours of data entry saved, overcharges caught before payment, money recovered after
payment through disputes, faster month-end close, audit-ready controls (SOX-style maker-checker), and fewer duplicate payments.
The app includes an **ROI calculator** (`/roi/`) that turns these into hours saved, labor saved, overcharges
caught, net annual benefit and payback period.

---

## 3. Case study (use this structure in proposals)

**Client profile (target persona):** a mid-size US importer or freight forwarder handling 200–4,000 shipping
documents a month, with a 2–6 person AP team using QuickBooks Online or Xero.

**Challenge**
- Documents arrive by email in every format: text PDFs, scanned PDFs, phone photos, Excel sheets, ZIP
  files, and carrier batch PDFs with 20 invoices in one file.
- No single reference ties documents together. A freight invoice may only list a container number, a commercial
  invoice only a PO.
- Carriers bill charges above the quoted rate, or add demurrage/detention nobody approved.
- Finance needs controls: whoever prepared a payment must not approve it, approvals need limits, everything
  must be auditable.

**Solution delivered**
1. **Omnichannel intake:** per-company forwarding addresses (Postmark/Mailgun webhooks), Microsoft 365 via
   Graph OAuth, IMAP, Gmail API, web upload and REST API, all deduplicated by Message-ID and SHA-256.
2. **Robust document reading:** content-based file detection, EXIF auto-rotation of photos, spreadsheet-to-text
   with table detection, zip-bomb-safe archive extraction, smart splitting of multi-invoice PDFs, OCR fallback
   (Claude vision or AWS Textract).
3. **Grounded AI extraction:** schema-constrained JSON output from Claude/OpenAI at temperature 0. Every value
   is verified against the source text. Ungrounded values are flagged as possible hallucinations. An offline
   regex rule engine provides a zero-cost fallback.
4. **Deterministic matching** by B/L (1.0), container (0.95), PO (0.85), credit-note link (0.9) or fuzzy
   near-match (0.7, always reviewed), with ISO 6346 container check-digit validation.
5. **38 validation checks** in plain code: totals vs line items, duplicates, amount outliers vs vendor
   history, containers missing from the B/L, over-quote charges, unapproved extra charges, US customs duty / MPF /
   HMF math, HTS code format, entered value vs invoices, free-time deadlines.
6. **Review UI with evidence highlighting:** click any extracted value and the PDF scrolls to it and highlights
   exactly where it was printed.
7. **Controls:** 4 roles, maker-checker, per-person approval limits with FX conversion (fails closed),
   mandatory reasons for overrides, immutable audit log, 2FA.
8. **Accounting sync:** idempotent bill and vendor-credit posting to QuickBooks Online or Xero with PDF
   attachments, plus hourly payment-status sync and AP aging.
9. **Recovery and close:** dispute letters generated from evidence, credit tracking, savings ledger,
   month-end accruals with confidence scores and Excel journal entries, vendor statement reconciliation.

**Results (measured on the bundled labelled benchmark, 20 shipments / 68 documents incl. 3 scans)**
- Document classification: **65/65 correct**
- Field extraction: **100%** across 17 fields (invoice numbers, totals, line items, containers, B/Ls, dates, parties, ports…)
- Shipment grouping: **100% pair precision and recall, 20/20 shipments exactly reconstructed**
- Planted errors caught: **6/6 (100%)**, with **0 false alarms**
- **65%** of shipments ready for approval with zero human touches
- Processing: 68 documents in ~31 s on a laptop, **$0.00** AI cost with the offline reader
- Scanned documents without OCR configured are correctly parked as "needs OCR" rather than guessed

> When quoting results, say *"on a labelled synthetic benchmark"*. These are not production customer metrics
> (see Section 14). The `run_eval` command reproduces them, and `pilot` / `accuracy_report` measure accuracy
> on real documents from reviewer corrections.

---

## 4. Complete capability list

### 4.1 Document intake (any channel, any format)
- **Channels:** web upload, REST API, Gmail API (label polling), **Microsoft 365 / Outlook** (Graph OAuth +
  PKCE), **IMAP** (Google Workspace, Yahoo, iCloud, Zoho…), **per-company forwarding address** via **Postmark**
  or **Mailgun** inbound webhooks, bulk folder import.
- **Formats:** text PDF, scanned PDF, JPG/PNG/TIFF/WebP photos (EXIF auto-rotate, multi-page TIFF), XLSX/CSV
  (header-row detection, column synonyms, total-row detection, 15 currencies), **ZIP archives** (each file becomes
  a document, never written to disk), **multi-invoice batch PDFs** split automatically (page-signal heuristics
  plus one optional AI call for uncertain boundaries, undoable).
- **File type detected from content, not the file name.** Friendly rejection messages for HEIC, Word, RAR and others.
- **Safety limits:** zip-bomb ratio checks, nesting depth, unpacked-size checks re-verified while streaming,
  megapixel caps, sheet row/column caps, defusedxml for XLSX.
- **Dedupe:** emails by Message-ID per org, files by SHA-256. Retries and duplicate mailboxes never double-import.
- **Email specifics:** nested MIME, attached .eml, RFC 2047/2231 encodings, IMAP modified UTF-7, UIDVALIDITY
  resync, BODY.PEEK (doesn't mark mail read), allowed-sender lists, signature-logo skipping, per-attachment
  outcome log, mailbox health and "needs reconnecting" states.

### 4.2 AI document understanding
- **8 document types:** commercial invoice, bill of lading, freight invoice, credit note, customs entry
  (CBP Form 7501 and foreign import declarations), arrival notice / delivery order, plus other/unknown.
- **Classification:** keyword scoring with title weighting and a confidence formula. Falls back to the LLM
  below 0.7 confidence.
- **LLM extraction:** Anthropic Claude (Sonnet/Haiku/Opus) or OpenAI, via raw HTTPS (no SDK lock-in),
  **JSON-schema-constrained output**, temperature 0, optional native PDF input for layout-heavy invoices,
  retries with backoff and Retry-After, **per-document token and cost tracking**.
- **Pydantic schemas** per document type, including line items, tariff lines (HTS, rate, duty) and per-container
  free-time dates. Lenient parsing drops one bad field instead of failing the whole document.
- **Grounding / anti-hallucination:** confidence comes from whether the value is literally present in the
  document (0.95 found / 0.50 not found), with number-, date- and text-aware comparison. It is not the model's
  self-reported confidence.
- **Offline rule engine** (regex + label proximity) as a zero-cost reader and as the automatic fallback when
  the LLM fails.
- **OCR:** PDF text layer first, then Claude vision OCR or **AWS Textract** (word boxes kept).
- **Vendor learning (human-in-the-loop ML):** learns from reviewer corrections per vendor: the label a vendor
  prints before a value, day-first vs month-first dates, recent mistakes. Vendors are recognised by fuzzy-matching
  the letterhead (no AI call). Learnings feed back into both the rule reader and the LLM prompt. Visible and
  forgettable in settings.
- **Evidence highlighting:** about 1,000 lines of no-AI code that locate each value's bounding box on the PDF
  (handles 1,234.50 / 1.234,50 / "USD 1,234.50", many date formats, wrapped names, label preference such as
  "total" over "subtotal"). The reviewer clicks a value and the in-app **PDF.js** viewer jumps there and highlights it.
- **AI also used for:** polishing dispute letters (rejected if any amount or reference is lost), classifying
  unknown charge names (cached), and optional HTS-code-vs-description sanity checks.
- **Evaluation harness:** `run_eval` scores classification, per-field accuracy, grouping precision/recall,
  error recall, false alarms and AI cost, and compares providers, models and input modes side by side.
  `accuracy_report` measures real-world accuracy from reviewer edits.

### 4.3 Matching and validation (deterministic)
- Shipment matching by B/L, container, PO, credit-note link, customs entry number, or fuzzy near-match, under
  per-organization row locks (concurrency-safe). Merge, move and manual override supported.
- **ISO 6346 container check-digit validation.**
- Checks: missing fields, totals vs line items, invalid container, low confidence, duplicate invoice, amount
  outlier vs vendor history (median-based), missing B/L or commercial invoice, container not on the B/L, fuzzy
  match, credit note larger than its invoice, duplicate credit note, vendor statement sent as an invoice.
  Issues are fingerprinted so resolved ones don't reappear.
- Every issue carries **money at risk**, which feeds the savings ledger.

### 4.4 Review and approval workflow
- Review queue with tabs, search, filters, payment status column, assignment filters.
- **Roles:** Viewer, Reviewer, Approver, Admin. Enforced on every view and API call.
- **Maker-checker** (segregation of duties), **per-person approval limits** in home currency with FX conversion
  that fails closed, written reasons for error overrides and rejections, locking of approved shipments.
- **Bulk approve / assign / post** with a preview page that explains each skip. One audit row per shipment with a batch ID.
- **Auto-assignment:** round-robin or per-vendor rules.
- **Threaded comments with @mentions**, in-app notification bell, email notifications.
- **Approve from email or Slack:** signed, expiring links to a phone-friendly approval page (they never bypass sign-in or 2FA).
- **Keyboard shortcuts** (j/k, x, a, r, e, g-then-q…) with screen-reader announcements.
- **Firm view for accounting firms:** "All clients" portfolio dashboard and "My work across clients".

### 4.5 Accounting integrations
- **QuickBooks Online:** OAuth 2.0, bills and vendor credits with PDF attachments, vendor matching and creation,
  expense account mapping, Class tracking, `requestid` idempotency, throttling and retry, safe rotation of the
  refresh token across workers, error codes translated into plain English, minorversion 75+.
- **Xero:** OAuth 2.0 with client secret **or PKCE (S256)**, multi-organisation tenant picker, ACCPAY bills and
  ACCPAYCREDIT credit notes, attachments, history notes, tracking categories, draft or approved mode,
  `Idempotency-Key` on every write, minute and daily rate-limit handling, token revocation on disconnect,
  granular-scope support.
- One **provider interface** for both. Switching providers is safe (the new connection must succeed first).
- **Payment status sync** (hourly, batched queries): unpaid, partly paid, paid, voided or deleted, plus credits
  applied. Paid bills are re-checked for 90 days to catch reversals.
  **AP aging dashboard** (not due, 1–30, 31–60, 61–90, 90+).
- Pluggable bill-line builders (for example, one line per shipment for shared invoices).

### 4.6 Rate auditing and savings
- Quote / rate-card management per vendor, lane (UN/LOCODE or port name, fuzzy-matched), equipment (20GP, 40HC,
  reefer, LCL…) and validity, with per-container / shipment / B/L / kg / cbm / day / hour charge bases.
- Approved accessorials (demurrage, detention, waiting time) with free days and caps.
- Charge-name normalisation (THC, BAF, LSS, CAF, ISF, D&D, PSS…): keyword table, then org-taught aliases,
  then AI (cached).
- Checks: **charged above quote**, **unapproved extra charge**, unchecked accessorial, no matching quote, quote currency mismatch.
  Configurable tolerance.
- **Savings ledger** that survives re-validation: prevented, still at risk, accepted, or withdrawn (never
  over-counts). Dashboard shows "overcharges caught this month".
- Atomic CSV import (all-or-nothing with per-row error report) and CSV export.
- **Public ROI calculator** for sales calls. Works without JavaScript.

### 4.7 Landed cost and shared invoices
- Per-product **landed cost**: freight, duties, fees and credits allocated over commercial-invoice lines by
  value, quantity, weight or volume (configurable per charge type), with fallbacks.
- **Largest-remainder rounding in whole cents**, so allocations always add up exactly.
- Frozen at approval with that day's FX rates. **Landed cost by product** report with trends and CSV/Excel export.
- **Shared invoices:** detects one forwarder invoice covering several shipments and splits it by lines,
  containers, weight/volume, equally, or by typed amounts, with an approval-ordering guarantee and posting blockers.

### 4.8 US customs compliance
- Reads **CBP Form 7501** entry summaries (numbered boxes, tariff lines, Chapter 99 lines, fee class codes) and
  foreign import declarations, plus arrival notices.
- Checks: duty per line vs rate x value, total duty, entry totals, entered-value totals, **MPF** (dated
  min/max table FY2018–FY2027 with Federal Register sources), **HMF** (0.125%), **HTS code format**, duplicate
  or changed entries, **entered value vs commercial invoices** (undervaluation risk), country of origin mismatch,
  optional AI HTS-vs-description check.
- **Free time / demurrage and detention tracking** per container: working vs calendar days, org holidays,
  time-zone-aware countdowns, **"last free day in 2 days"** and **"free time passed"** alerts with estimated daily cost.
- Duty flows into landed cost automatically.

### 4.9 Vendor disputes and recovery
- One-click **dispute letters** generated from the evidence (amounts, invoice, containers, quote reference),
  optional AI polish with an integrity check, sent by email with the PDF attached and the Message-ID tracked.
- Lifecycle: draft, sent, acknowledged, credit received, resolved, or closed. Follow-up dates, overdue flags,
  timeline, money recovered per month and quarter.
- Incoming credit notes are **auto-linked** to the dispute they answer. An approver confirms.
- Disputes block approval unless an approver overrides with a note.

### 4.10 Month-end close
- **Accruals report:** "received, not booked" plus "shipped, not yet invoiced" estimated from quotes, then the
  vendor median, then the org median, each with a **confidence score** and an explanation.
- **Excel journal entry** with a reversing entry, per account and vendor. CSV export. Approvers adjust lines with audit.
- **Period locking** with versioning, SHA-256 fingerprint and drift comparison.
- **Vendor statement reconciliation** (PDF/XLSX/CSV, AI or rules): matched, amount differs, missing, not on statement, unapplied
  credits, duplicates, unapplied payments, unknown payments. The explanations add up to the balance difference exactly.
- Payments recorded manually or **read from QuickBooks**.

### 4.11 Notifications and integrations
- Alerts to **Slack** (Block Kit), **Microsoft Teams** (Adaptive Cards via Power Automate) and **HTML email**:
  needs review, ready to approve, posting failed, reconnect needed, bill voided, dispute overdue, credit
  received, free-time alerts, plus a **daily digest** at each org's local hour.
- Delivery after the transaction commits, retry with backoff, Retry-After honoured, stalled-delivery sweeper.
- **Outgoing webhooks:** 15+ event types, Stripe-style HMAC-SHA256 signatures, secret rotation with overlap,
  versioned envelopes, exponential retry up to 6 h, auto-disable after repeated failures, delivery log with replay,
  **SSRF protection with DNS-rebinding defence** (IP pinning).
- **REST API** (Django Ninja, OpenAPI docs at `/api/docs`): scoped API keys (`shipments:read`, `documents:read`,
  `exports:read`, `documents:write`), hashed storage, expiry, per-minute rate limit.
- **Streaming CSV and Excel exports** (row-by-row, 200k rows), protected against formula injection, audited.

### 4.12 SaaS platform features
- **Multi-tenancy:** every record scoped to an organization, membership-based access, tenant-isolation tests. **Even platform superusers can't touch a client's data** without being added as a member, which shows in that client's audit log. The platform admin is read-only for client records.
- **Self-serve signup:** email verification with signed single-use links, honeypot, IP rate limits (IPv6 /64
  aware), disposable-domain blocking, account-enumeration resistance.
- **Stripe billing without the SDK:** Checkout, Customer Portal, verified webhooks (HMAC, multi-secret
  rotation, replay window, exactly-once events), subscription state machine robust to out-of-order events,
  **usage metering** with soft (100%) and hard (120%) limits. Plans: Starter $199 / Growth $499 / Scale $1,290 per month, 14-day trial.
- **Onboarding checklist** that ticks itself from real state (not from clicks).
- **Public demo mode:** nightly reset, guard rails, nothing ever sent outside the server. **"Try it" page**:
  anonymous single-PDF sandbox with per-IP and global caps that bound AI cost, auto-deleted after 24 h.
- Dashboard with documents per day, AP aging, savings, containers near their last free day. Accessible CSS charts with table fallbacks.

---

## 5. Security and compliance highlights

- **2FA (TOTP)** with QR enrollment and hashed single-use recovery codes. Orgs can require 2FA.
- **Encryption at rest (Fernet)** for OAuth tokens, 2FA secrets, webhook URLs and secrets.
- Login lockout (per user and per IP), 30-minute idle timeout, 12+ character passwords with Django validators.
- **Strict Content Security Policy** (no inline scripts), HSTS, secure cookies, Permissions-Policy, COOP, request IDs.
- **Immutable audit log** of every sign-in, edit, decision, export and setting change (actor, IP, request ID). CSV export.
- **Zero-trust platform admin:** operator accounts manage organizations, memberships and billing only. Client records are read-only in `/admin/`, org controls are locked once created, and support access is an audited membership.
- **SSRF guards** on webhooks, alert channels and IMAP hosts. HMAC verification on every inbound webhook
  (Stripe, Mailgun, Postmark basic auth).
- CSV/Excel **formula-injection protection**. Zip-bomb and decompression limits. defusedxml.
- Idempotency everywhere money moves (QuickBooks `requestid`, Xero `Idempotency-Key`, Stripe idempotency keys).
- Fail-closed approval logic. Segregation of duties.

---

## 6. Tech stack

| Layer | Technologies |
| --- | --- |
| Language | Python 3.12 (3.10–3.14 supported) |
| Web framework | **Django 5.2 LTS**, **Django Ninja** (REST API + OpenAPI), server-rendered templates, vanilla JS, vendored **PDF.js** |
| Data | **PostgreSQL 16** (SQLite for local), dj-database-url |
| Async / jobs | **Celery 5** + **Redis 7**, Celery beat (10+ scheduled jobs) |
| AI / ML | **Anthropic Claude API** (Sonnet, Haiku, Opus), **OpenAI API**, structured JSON-schema outputs, **AWS Textract** OCR, **rapidfuzz** fuzzy matching, **Pydantic v2** schemas |
| Documents | pdfplumber, pypdf, reportlab, Pillow, openpyxl, defusedxml |
| Integrations | QuickBooks Online API, Xero API, Stripe API, Microsoft Graph, Gmail API, IMAP, Postmark, Mailgun, Slack, Microsoft Teams |
| HTTP | httpx (all third-party APIs called directly, with retries, idempotency and mock transports in tests) |
| Security | cryptography (Fernet), pyotp (TOTP), segno (QR), Django signing |
| Storage | Local or **S3-compatible** (AWS S3, MinIO) via django-storages |
| Infra / DevOps | **Docker**, Docker Compose (dev and prod overlays), **gunicorn**, **Caddy** (automatic HTTPS), WhiteNoise, **DigitalOcean** one-command deploy script (firewall, secrets, releases, **rollback**, nightly `pg_dump` backups with rotation) |
| Quality | **pytest + pytest-django (844 tests)**, httpx.MockTransport fakes for every external API, **Ruff** linting, synthetic ground-truth dataset generator, accuracy evaluation harness |
| Observability | JSON structured logs, request IDs, `/health/` and `/health/ready/` probes |

---

## 7. Architecture (one paragraph and a diagram)

```
 Email (M365 / IMAP / Gmail / Postmark / Mailgun)   Upload / REST API
                    └──────────────┬─────────────────────┘
                         ingest_bytes (dedupe, type sniff, ZIP/photo/sheet/PDF-split)
                                   │  Celery
                text layer ─► OCR (Claude / Textract) ─► classify ─► extract (LLM grounded | rules)
                                   │                        ▲ vendor learning
                         locate values on page (evidence boxes)
                                   │
                match to shipment (B/L > container > PO > fuzzy; row-locked)
                                   │
           validate: totals · duplicates · outliers · rates · customs · free time  ─► savings ledger
                                   │
               review queue ─► maker-checker approval (roles, limits, 2FA)
                                   │
          QuickBooks / Xero bill + PDF (idempotent) ─► hourly payment sync ─► AP aging
                                   │
         alerts (Slack/Teams/email) · webhooks · API · exports · disputes · month-end close
```

Modular monolith: 18 Django apps with clear boundaries. Apps extend each other through **registries/plugins**
(check registry, charge sources, bill-line builders, posting blockers, webhook events, onboarding checks), not
hard imports. Background work is in Celery with retries. All money math uses `Decimal`.

---

## 8. Engineering quality signals

- **844 automated tests** across 41 files, **all green** (843 passed, 1 skipped, 0 failures; full run ≈ 9 min, verified 2026-10-03). Every external API (Stripe, QuickBooks, Xero, Microsoft Graph,
  LLMs) is faked with `httpx.MockTransport`. Tenant isolation, RBAC, security hardening and money math all have dedicated suites.
- **Labelled evaluation harness** with a synthetic data generator. It produces fictional shipments in multiple
  layouts, scans, photos, ZIPs, batch PDFs, customs forms and vendor statements, with **planted errors**
  and ground truth, so accuracy is measured, not asserted.
- **Idempotent, retry-safe integrations:** row-locked token rotation, exactly-once webhook processing,
  out-of-order event tolerance.
- **Fail-closed design** for approvals and currency conversion. "A wrong highlight is worse than none."
- **Extensibility through registries** rather than coupling.
- **Operational readiness:** health and readiness probes, JSON logs, one-command deploy, rollback,
  backups and restore, demo mode, ruff config.
- **Accessibility:** keyboard navigation, ARIA live announcements, chart table fallbacks.

---

## 9. Skills demonstrated (keywords for job matching)

**AI / LLM:** LLM application development · document AI / intelligent document processing (IDP) · OCR ·
structured outputs / JSON schema · prompt engineering · hallucination mitigation / grounding · RAG-style
context injection (vendor notes) · human-in-the-loop learning · LLM evaluation and benchmarking · LLM cost
tracking · multi-provider (Claude, OpenAI) abstraction · AWS Textract · Pydantic.

**Backend:** Python · Django · Django REST / Django Ninja · REST API design · OpenAPI · PostgreSQL · Celery ·
Redis · background jobs and scheduling · multi-tenant SaaS architecture · RBAC · concurrency (row locks) ·
idempotency · webhooks (inbound and outbound) · OAuth 2.0 / PKCE · email processing (IMAP, MIME, Graph API).

**Integrations:** QuickBooks Online API · Xero API · Stripe (subscriptions, metering, webhooks) · Microsoft
365 / Graph · Gmail API · Postmark · Mailgun · Slack · Microsoft Teams · AWS S3 / MinIO.

**FinTech / domain:** accounts payable automation · invoice processing · three-way document matching (commercial invoice, B/L, freight invoice) ·
bill pay sync · AP aging · month-end close and accruals · journal entries · vendor reconciliation ·
SOX-style controls (maker-checker, approval limits, audit trail) · multi-currency · logistics and freight
(B/L, containers, ISO 6346, demurrage and detention) · US customs (CBP 7501, HTS, MPF, HMF) · landed cost.

**Security:** 2FA/TOTP · encryption at rest · CSP · SSRF prevention · HMAC signature verification ·
rate limiting · abuse prevention · audit logging · OWASP-minded input handling.

**DevOps:** Docker · Docker Compose · Caddy / HTTPS · gunicorn · DigitalOcean · Bash deploy automation ·
backups and rollback · health checks · structured logging.

**Quality:** pytest · test doubles and mocking · synthetic data generation · evaluation metrics (precision, recall) · Ruff.

**Frontend:** Django templates · vanilla JavaScript · PDF.js integration · accessible UI · responsive approval pages.

---

## 10. Numbers cheat-sheet

| Metric | Value |
| --- | --- |
| **Total hand-written code** | **~65,800 lines** (excludes auto-generated migrations and vendored PDF.js) |
| Application Python (excluding migrations and tests) | ~42,400 lines |
| Test code | ~12,900 lines, **844 tests**, 41 files (test-to-code ratio ≈ 0.3) |
| Templates (HTML) | ~6,900 lines across 154 templates |
| Frontend CSS + JS (own code) | ~1,300 + ~1,400 lines (custom design system, no framework) |
| Deploy / ops (Dockerfile, Compose, Caddy, deploy and backup scripts) | ~800 lines |
| Documentation | 1,200-line operator README plus this brief |
| Django apps | 18 |
| URL routes | 213 |
| Document types understood | 6 business types (+ other/unknown) |
| Input file formats | PDF, JPG, PNG, TIFF, WebP, XLSX, CSV, ZIP |
| Email intake channels | 5 (forwarding via Postmark or Mailgun, Microsoft 365, IMAP, Gmail) |
| Accounting systems | 2 (QuickBooks Online, Xero) |
| Validation / check types | 38 distinct issue checks |
| Scheduled background jobs | 10+ |
| Outgoing webhook event types | 15+ |
| Benchmark field accuracy / grouping / error recall | 100% / 100% / 100%, 0 false alarms (synthetic, rules reader) |
| Benchmark straight-through rate | 65% of shipments need no human touch |
| Supported LLMs | Claude Fable/Opus/Sonnet/Haiku, OpenAI GPT-4o-mini (configurable) |

---

## 11. Demo and proof

- **Local demo in one command (Windows):** `powershell -ExecutionPolicy Bypass -File scripts\quickstart.ps1`.
  macOS/Linux: see README. Demo logins: `admin` / `reviewer` / `approver` (passwords in README).
- **Reproduce the benchmark:** `python manage.py run_eval --dataset datasets/synthetic`
  (add `--provider anthropic --model claude-haiku-4-5` to compare LLMs).
- **Try a single file:** `python manage.py try_extraction invoice.pdf` shows every value, grounding, tokens and cost.
- **API docs:** `/api/docs`. **ROI calculator:** `/roi/`. **Public demo mode:** `DEMO_MODE=1` with nightly reset.
- Good screens to show: review queue → shipment page with **evidence highlighting** → approval panel
  (maker-checker / limit explanations) → Savings dashboard → Month-end accruals → Disputes timeline.

---

## 12. Which features to lead with, by job type

| Job / project type | Lead with | Also mention |
| --- | --- | --- |
| **AI / LLM engineer, document AI, IDP, OCR** | Grounded extraction, structured outputs, rules fallback, evaluation harness with precision/recall, vendor learning, cost tracking, multi-provider | Evidence highlighting, batch PDF splitting, Textract |
| **Django / Python backend** | 18-app modular monolith, Django Ninja API, Celery jobs, multi-tenancy, RBAC, 844 tests | Idempotency, row locks, registries/plugins |
| **SaaS MVP / full product build** | Signup → trial → Stripe billing → usage limits → onboarding → demo mode → deploy script | Webhooks, API keys, alerts, firm view |
| **FinTech / accounting automation / AP** | QuickBooks + Xero posting, payment sync, AP aging, maker-checker, approval limits, audit log, month-end accruals, statement reconciliation | Disputes, savings ledger, multi-currency |
| **QuickBooks / Xero integration** | OAuth (incl. PKCE), idempotent bills/credits, attachments, rate limits, token rotation under row locks, payment read-back | Error translation, provider abstraction |
| **Stripe / payments integration** | SDK-free Stripe: Checkout, Portal, signed webhooks, exactly-once, out-of-order-safe state machine, metered limits | Trials, seat limits, dunning emails |
| **Logistics / freight / supply chain** | B/L, container, PO matching, ISO 6346, rate-card auditing, demurrage/detention free-time alerts, shared forwarder invoices, landed cost | Arrival notices, accessorial caps |
| **Customs / trade compliance** | CBP 7501 reading, duty/MPF/HMF checks with a dated fee table, HTS validation, undervaluation check, country-of-origin check | Duty in landed cost |
| **Email automation / integrations** | Microsoft Graph, IMAP, Gmail, Postmark/Mailgun webhooks, MIME edge cases, dedupe | Allowed senders, mailbox health |
| **Security-focused roles** | 2FA, encryption at rest, CSP, SSRF + DNS-rebinding defence, HMAC webhooks, rate limits, audit log, enumeration resistance | Zip-bomb limits, formula-injection protection |
| **DevOps / deployment** | Docker Compose dev/prod overlays, Caddy HTTPS, one-command droplet deploy with rollback, backups and restore | Health probes, JSON logs |
| **Workflow / internal tools** | Review queue, bulk actions with preview, assignment rules, comments/@mentions, keyboard shortcuts, approve-from-Slack links | Notifications, exports |
| **Data / reporting** | Streaming CSV/Excel exports, savings ledger, landed cost by product, accruals journal, accuracy reports | Dashboard charts |

---

## 13. Reusable proposal paragraphs

**Reliability with AI**
> In ShipMatch I treated the LLM as a reader, not a decision-maker. Every value the model returns must be found
> in the source document or it is flagged for review, and all matching and money math is deterministic code
> with tests. That's how I'd approach your project too: AI where it adds leverage, guardrails where mistakes cost money.

**Integrations done properly**
> My QuickBooks and Xero integrations are idempotent (a retry or double-click never creates a duplicate bill),
> rotate OAuth refresh tokens safely across parallel workers, respect rate limits including Xero's daily cap,
> and translate API errors into plain English for finance users.

**Measured, not guessed**
> I built an evaluation harness with a synthetic ground-truth dataset, including planted errors, so extraction
> accuracy, grouping precision/recall and AI cost are measured on every change. On that benchmark the system
> scores 100% field accuracy and catches every planted error with zero false alarms.

**Production-minded**
> ShipMatch ships with 2FA, encryption at rest, a strict CSP, an immutable audit log, SSRF-safe webhooks,
> Stripe billing, a public demo mode, and a one-command deploy with rollback and nightly backups. It's covered by 844 automated tests.

---

## 14. Honesty guardrails (do NOT claim)

- **No production customers or revenue are documented.** Present ShipMatch as a portfolio / product build, not
  as "used by N companies". Benchmark numbers come from a **synthetic labelled dataset** and the offline rule reader.
- The **100% accuracy** figure applies to that benchmark, not to arbitrary real-world documents. For real documents
  the product measures accuracy via `pilot` / `accuracy_report`.
- **No CI pipeline is configured in the repo** (the README mentions `.github/workflows/ci.yml`, but it doesn't
  exist). Say "comprehensive automated test suite", not "CI/CD pipeline".
- UI is **server-rendered Django + vanilla JS**, not React/Vue/SPA.
- Prices ($199/$499/$1,290) are configurable defaults, not market-validated pricing.
- Customs logic is **US-centric** (CBP 7501, MPF/HMF). Other countries' declarations are read but only generic checks apply.
- Old `.xls`, HEIC, Word and RAR files are not supported (users are asked to convert).
- Celery workers don't run natively on Windows (Docker or inline mode is used there).
