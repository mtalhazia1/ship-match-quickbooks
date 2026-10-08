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
| **QA-085** month-end Excel journal | 18.1 s | 16–21 s | Not improved: 8 of the seconds are spent by `openpyxl` saving 127,000 cells, which is slow without the optional `lxml` library (not installed here). Adding `lxml` to `requirements.txt` is the cheapest fix; I did not install it without asking. |
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
