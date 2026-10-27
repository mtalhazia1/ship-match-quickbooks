# ShipMatch QA — Session 13: behaviour at scale (8 Oct 2026)

Same rules: **issues listed, nothing fixed**. SQLite development database (a faster engine for reads than Postgres under a server, so timings below are on the optimistic side for query-heavy pages), measured with Django's test client in-process: wall time and number of database queries per request. Branch under test: `fix/qa-session-08`.
Starting DB backup: `%TEMP%\db.sqlite3.bak-session13-start`; **restored at the end**. Issue ids continue from session 12 (last was QA-084).

## 1. Method

3,000 synthetic shipments were added to the Acme demo organization (status mix: 5/9 needs review, 2/9 ready, 1/9 approved, 1/9 posted), with 6,000 documents, 30,000 extracted fields and about 3,450 open issues, on top of the 34 shipments already in the database. Each page was requested once to warm it, then timed.

## 2. Results

| Page / download | Time | Queries | Size | Verdict |
|---|---|---|---|---|
| Review queue (any tab, search, page 60) | 0.24–0.42 s | 24 | 19–38 KB | ✅ paged, constant queries |
| Documents list | 0.15–0.25 s | 19 | 16–25 KB | ✅ |
| Global search | 0.26 s | 16 | 11 KB | ✅ |
| Disputes, rates, team, notifications, audit log | 0.06–0.18 s | 16–28 | 11–35 KB | ✅ |
| Statements | 0.28 s | 24 | 19 KB | ✅ |
| API shipments list (200 rows) | 0.09 s | 7 | 58 KB | ✅ |
| API issues export | 2.2 s | 8 | 635 KB | 🟡 |
| Dashboard | 1.8–2.0 s | 151 | 29 KB | 🟡 QA-089 |
| Savings & ROI | 2.5 s | 24 | 33 KB | 🟡 QA-089 |
| **Month-end accruals report** | **16.2 s** | 41 | **6.3 MB** | ❌ QA-085 |
| Month-end CSV / Excel journal | 10.7 s / 18.1 s | 35 / 33 | 1.6 MB / 0.6 MB | ❌ QA-085 |
| **My work** | **9.4 s** | **1,984** | 27 KB | ❌ QA-086 |
| **Landed cost report** | **7.9 s** | **2,018** | 48 KB | ❌ QA-087 |
| Landed CSV / Excel | 8.0 s / 10.8 s | ~2,010 | 5–10 KB | ❌ QA-087 |
| API documents list (200 rows) | 1.0 s | 209 | 154 KB | 🟡 QA-088 |
| API shipments / documents export | 10.2 s / 12.1 s (Excel 14.7 s) | 75 / 39 / 72 | 0.3–1.6 MB | ❌ QA-088 |

## 3. New issues

**QA-085 — High at scale — The month-end accruals report takes 16 s and sends a 6.3 MB page; its CSV and Excel journal take 10–18 s**
The report is built from scratch on every view and every download, and the page lists every line with no paging. A profile of the CSV shows 16 s inside `close.services.accruals.build`: 11.4 s in `history.load` (reading the history of every vendor), 3.5 s in `_received`, 1 s in `_not_invoiced`; only 33–41 queries, so it is Python work over the whole data set. A real month of a few thousand shipments will make the page time out behind a proxy (typically 30–60 s) and the 6 MB table hard to use. Build the vendor history once (and cache it), page or collapse the lines, and build exports from the same cached result.

**QA-086 — Medium — "My work" runs about 2,000 queries (9.4 s) for roughly 300 items**
Per item it runs one query for the open-error count (about 300) and one audit-log lookup (about 300), plus more. Count issues with one grouped query and read the audit rows in one query for all items. This is the remaining part of QA-056.

**QA-087 — withdrawn (see 3b) — The landed-cost report and its CSV/Excel run about 2,000 queries (8–11 s)**
For each of the first 500 shipments it loads the documents (500 queries) and then their fields (500 queries), and a similar number again for other lookups. Use `prefetch_related("documents__fields")` or a single query per table.

**QA-088 — Low/Medium — API documents list and the three export endpoints are slow at scale**
`/api/{org}/documents` runs one extra query per document just to list its child ids (209 queries for 200 rows). The shipments and documents exports took 10–12 s (Excel 15 s) for 3,000 shipments and 6,000 documents, mostly reading each document's fields one at a time in Python.

**QA-089 — Low — The dashboard (1.8 s, 151 queries) and Savings (2.5 s) are noticeably slow at this size**
Both are well inside "usable" but grow with data; the dashboard's 151 queries should be about 20.

## 3b. Fixed afterwards (same day) and one finding withdrawn

Same 3,000-shipment data set, same machine; timings move by a few tenths of a second between runs.

| Item | Before | After | What changed |
|---|---|---|---|
| **QA-085** month-end report page | 16.2 s, 6.3 MB | **5.8 s, 143 KB** | The lines table and the "Not accrued" table are paged (100 and 50 rows); the CSV and Excel downloads still hold every line. Building the report is faster: the whole-organization read no longer builds a field object for each of 30,000 values or loads every document's OCR text (bills of lading only), two lookups that rescanned every sample are indexed, and repeated date strings are parsed once. Output checked **byte-identical** to the original code for all 6,042 lines. |
| **QA-085** month-end CSV | 10.7 s | 6.3 s | Same build speed-up. |
| **QA-085** month-end Excel journal | 18.1 s | 16–21 s | Not improved: about 8 of the seconds are spent by `openpyxl` saving 127,000 cells (roughly 44 µs per cell). **`lxml` was tried (6.1.3, installed and used by openpyxl) and made no measurable difference** on this machine (8.8 s with it, 8.2 s without, identical cell values and formats), so it is not a fix. A real saving needs a different approach, for example writing the Lines sheet in openpyxl's write-only mode or as CSV. |
| **QA-086** My work | 9.4 s, 1,984 queries | **0.55–0.85 s, 193 queries** | Error counts, "who prepared this" and approval-limit totals are read for all ready shipments at once; the dispute-hold and shared-invoice rules run only for the few shipments that have one (`approval_blockers(..., hints)`; the answers are unchanged, checked in a test for clean, error, dispute-held, maker-checker and over-limit shipments). |
| **QA-087** landed report and exports | 7.9 s, 2,018 queries | 0.4 s, 18 queries | **Finding withdrawn as a defect.** The cost was a one-time catch-up: the page "freezes" up to 100 approved shipments per view that have no saved landed-cost result, and my synthetic shipments were set to *approved* directly, bypassing the freeze that normally happens at approval (`landed/hooks.py`). After five views everything was frozen and the page took 0.26–0.4 s. Remaining note: on an installation upgraded with thousands of old approved shipments, the first views each spend several seconds catching up. |
| **QA-088** API documents list | 1.0 s, 209 queries | **0.45–0.5 s, 9 queries** | The list endpoint already fetched each document's children; the document schema asked the database again for each row. |
| **QA-088** API exports | shipments 10.2 s, documents 12.1 s | 7.4 s, 8.0 s (about 9.5 s on a busy run) | The export links no longer resolve the URL pattern for every row; the shipments export reads only the two values its totals need. The rest is the cost of building about 50,000 Django objects; a further saving needs the exports rewritten on plain queries. |
| **QA-089** dashboard / Savings | 1.9 s / 2.5 s | 1.7 s / 1.6 s | The savings summary no longer builds a shipment object for every catch (it reads their statuses in one query) and loads documents only for the rows shown. Both pages grow with the number of catches in the month (3,000 here). |

## 4. Notes (not issues)

- Paged lists (queue, documents, audit, notifications, disputes, rates) stay at 16–28 queries and under half a second at 3,000 shipments: the paging fix from session 07 holds.
- Timings were taken with the in-process test client and no cache warm-up for the heavy reports; a deployed server with Postgres and Redis will differ, but query counts and the Python-bound profile above will not.

## 5. Still untested

Real webhook delivery and replay; real IMAP / Microsoft / Gmail mailboxes; QuickBooks disconnect / reconnect and Xero connect; other browsers and a screen reader; posting to QuickBooks twice at once; Redis and Celery workers; performance on Postgres with the same data (the Postgres container was removed); memory use of the 6 MB report page in a browser.

## 6. Test data left behind

None: the database was restored from the starting backup (the 3,000 synthetic shipments are gone).

---

## 7. Continuation (12 Oct 2026): re-check of the fixes and the pages not timed on 8 Oct

Branch under test: `fix/qa-session-13` at `80d46bf`. Same 3,000-shipment data set (same loader, seed 7), loaded into a **copy** of the database in a scratch folder through `DATABASE_URL`, so `db.sqlite3` was never changed. 55 pages, downloads and API calls, each requested once to warm and then timed, signed in as `admin`. Shipment exports were run with `status=all` and document exports with `view=all`; the defaults (`needs_review`, `attention`) leave out the synthetic rows, which explains a 2 KB documents export in the first run.

### 7a. New issue

**QA-090 — Medium (SQLite) — The Savings CSV export returns HTTP 500 once the period holds about 2,000 catches. This is a regression from the QA-089 fix (`ebff5e8`).**
`/savings/export.csv` calls `summary(..., limit=None)`. The QA-089 change replaced `select_related("shipment", "document")` with `prefetch_related_objects(...)` over every catch row. SQLite then fails with `OperationalError: Expression tree is too large (maximum depth 1000)` (`apps/rates/savings.py:322`). Measured on the same data: 1,500 catches work, 2,000 fail, and the org had 3,024. On the commit before the fix (`92c0b91`, run in a temporary worktree against the same data) the export returned **200 in 0.76 s**. The Savings page itself is fine because it shows only 25 rows. PostgreSQL has no such limit (not re-run there). Repro: load the session 13 data set, sign in as admin, open Savings, click Export CSV.

**Fixed (12 Oct).** The actual limit is on distinct shipments, not catches. Django prefetches this foreign key as `id = 1 OR id = 2 OR …`, one term per distinct shipment. Here there were 1,134 shipments, so 1,133 `OR`s, and SQLite allows at most 1,000. The prefetch now runs in batches of 500 catches. On the same data the export returns **200 in 0.49 s with 20 queries**, the same 348 KB file as before the regression. A test exports 1,100 catches on 1,100 shipments; it fails without the fix.

### 7b. Re-check of the 8 Oct fixes (all hold)

| Item | 8 Oct after fix | 12 Oct | Note |
|---|---|---|---|
| My work | 0.55–0.85 s, 193 q | 0.34 s, 193 q | ✅ |
| Month-end report page | 5.8 s, 143 KB | 2.1–2.3 s, 144 KB | ✅ paged |
| Month-end CSV | 6.3 s | 2.2 s | ✅ |
| Month-end Excel journal | 16–21 s | 5.3–10.2 s | 🟡 still the slowest download after the documents Excel (QA-085 rest) |
| API documents list (200 rows) | 0.45 s, 9 q | 0.16–0.22 s, 9 q | ✅ |
| API shipments / documents export (CSV) | 7.4 / 8.0 s | 3.8 / 3.6 s | 🟡 QA-088 rest |
| API documents export (Excel) | 14.7 s before fix | **17.8 s** | 🟡 slowest download in the app; openpyxl cell writing, as for the journal |
| Web shipments / documents export | not timed | 4.4 / 4.8–8.0 s | 🟡 same code as the API |
| Dashboard / Savings | 1.7 / 1.6 s | 0.8 / 0.8 s, 152 / 27 q | ✅ dashboard still ~150 queries (QA-089) |
| Landed report / landed CSV | 0.4 s once caught up | 3.1 s, 2,018 q / 5.8 s, 2,010 q | As in 3b: the synthetic approved shipments are frozen 100 per request. The **CSV download also does this catch-up**, so a GET export writes to the database. Not re-raised; see the QA-087 note. |

Machine load differs between days, so compare query counts rather than seconds.

### 7c. Pages timed for the first time at 3,000 shipments (no issues)

Under 0.5 s and at most 30 queries: clients, month-end settings, customs settings, ROI, rates (list, rules, extras, charge names, CSV), audit CSV (285 KB, 0.28 s), every settings page, team, notifications, disputes list and detail, statements, review queue with search, documents search, the API shipment and document detail, the quick-approval panel, and the per-shipment landed CSV. Others:
- **Shipment page:** 0.18–0.24 s with 71–89 queries. It does not grow with the size of the organization.
- **Free-time page:** 169 queries, 0.36–0.74 s. It is paged, so the count is bounded.
- **Accuracy report:** 0.67 s.
- **Issues export:** 0.9–1.3 s for 635 KB.

### 7d. Other checks

- `makemigrations --check`: no model changes without a migration. `ebff5e8` touched `documents/models.py` but changed no fields.
- Automated suite on the combined `main` (12 Oct): about 1,090 passed, 1 skipped, none failed (SQLite); `ruff check` clean.

### 7e. Combining with `mtalhazia1/ship-match-quickbooks` (fixed the same day)

The session 08–13 commits were replayed onto `main` of `mtalhazia1/ship-match-quickbooks`. That repository had fixed QA-056 and QA-063 independently. For QA-063 both sides made the same change. For QA-056/QA-086, its `prefetch_approval` design was kept and the `bulk_hints` version from section 3b was dropped; the session 13 test now checks `prefetch_approval` gives the same answers as checking one shipment at a time.

Timing **My work as `approver`**, who has a USD 50,000 approval limit, showed that **both versions still ran about 7 queries per ready shipment** for a user with a limit. Section 3b had been measured only as `admin`, who has no limit. With 652 ready shipments: `mtalhazia1` version 2,001 queries and 2.25 s, `fix/qa-session-13` 1,407 queries and 1.61 s. The cause was `Document.field()`, which ran its own query and ignored `prefetch_related("fields")`, so the per-document field reads in `shipment_totals` were never batched. It now uses prefetched fields, and `prefetch_approval(with_totals=True)` loads every shipment's documents at once. Result: **50 queries, 0.26 s** as `approver`, and 88 queries, 0.17 s as `admin`. Two tests cover it: My work's query count does not grow with shipments when the approver has a limit (it failed before the fix), and `field()` makes no query on prefetched fields.

### 7f. Excel downloads switched to XlsxWriter (13 Oct)

A profile of the documents Excel export showed nearly all its time in openpyxl writing 372,771 cells (6,111 rows × 61 columns) one at a time; the database part was small, and write-only mode was already in use. A benchmark on the same shape: openpyxl 13.7 s, XlsxWriter 5.7 s. The three Excel writers (exports, month-end journal, landed cost) now use XlsxWriter through `apps/core/xlsx.py`. The seven Excel downloads were saved before and after the change and compared cell by cell (about 400,000 cells; values, types, number formats, bold, font size, fills, column widths, frozen panes): identical.

| Download | Before | After | Size before → after |
|---|---|---|---|
| Documents Excel (API and web) | 15.7–16.1 s | 6.2–6.3 s | 1,240 → 469 KB |
| Month-end journal | 7.8 s | 4.6 s | 571 → 530 KB |
| Shipments Excel | 7.0 s | 4.3 s | 348 → 253 KB |
| Issues Excel | 3.1 s | 1.6 s | 199 → 167 KB |

### 7g. Test data left behind

None. All work ran on a scratch copy of the database, and the temporary worktree was removed.
