# ShipMatch QA — Session 20: load and performance on PostgreSQL (16–17 Oct 2026)

Same rules: **issues listed, nothing fixed**. The session 13 data set (same loader, `%TEMP%\bulk.py`, seed 7: 3,000 synthetic shipments, 6,000 documents, 30,000 fields, 3,453 issues) on top of the demo data, in two places: (1) the throwaway PostgreSQL 16 container of session 18, timed in-process with Django's test client (one warm-up, then one timed request) for comparison with session 13's SQLite numbers; (2) the production stack of session 19 (Caddy → gunicorn 3 sync workers → Postgres, Redis, prefork worker), loaded over HTTPS by a threaded Python client with 25 signed-in users. Branch under test: `merge/qa-sessions-08-13` at `b94b52f`. Issue ids continue from session 19 (last was QA-104).

## 1. Pages and downloads on PostgreSQL (in-process, 3,000 shipments)

| Page / download | Postgres (today) | SQLite (session 13, 12 Oct) |
|---|---|---|
| Review queue (any tab, page 60) | 0.14–0.15 s, 24 q | 0.24–0.42 s, 24 q |
| Queue search | 0.34 s | — |
| Documents list / search over all | 0.08 / 1.04 s, 19 q | 0.15–0.25 s |
| Global search | 0.87 s, 16 q | 0.26 s |
| Dashboard | 1.2–1.9 s, 97–100 q (see note) | 0.8 s, 152 q |
| My work (admin) | 1.27 s, 77 q | 0.34 s, 193 q |
| Savings / Savings CSV | 1.30 s / 0.92 s (348 KB) | 0.8 s / 0.49 s |
| Month-end report / CSV / Excel journal | 2.5 / 2.6 / **4.5 s** | 2.1–2.3 / 2.2 / 5.3–10.2 s |
| Audit list / audit CSV | 0.12 / 0.59 s | — / 0.28 s |
| Landed report / CSV / Excel (after catch-up) | 0.16–0.19 s, 10–18 q | 0.4 s |
| Free-time page | 0.41 s, 78 q | 0.36–0.74 s, 169 q |
| API shipments list / documents list (200 rows) | 0.08 s, 7 q / 0.17 s, 9 q | 0.09 / 0.16–0.22 s |
| API shipments export CSV / Excel | 3.3 / 4.6 s | 3.8 s / — |
| API documents export CSV / Excel | 4.3 / **5.8 s** | 3.6 / 17.8 s (before XlsxWriter) |
| API issues export | 0.67 s | 0.9–1.3 s |
| Everything else timed (ROI, rates, disputes, team, notifications, statements, accuracy, clients) | 0.06–0.83 s, 14–28 q | similar |

No server errors. The Excel downloads are 2–3× faster than in session 13 (XlsxWriter, commit `b94b52f`). The landed-cost catch-up (session 13, QA-087 note) is still there on Postgres: the first six views took 8.6, 6.9, 6.2, 6.4, 6.2 and 6.2 s while they froze 100 approved shipments each.

Note on the dashboard: in the timed pass it read 5.4 s with **0 queries recorded**; three repeats straight after gave 1.87, 1.19 and 1.20 s with 97–100 queries. The first reading isn't explained (possibly the cache being filled by a parallel request); not raised.

## 2. Many users at once (production stack, HTTPS through Caddy)

| Phase | Result |
|---|---|
| **A. 25 sign-ins at the same moment** | All succeeded; 8.9 s wall; per sign-in p50 4.4 s, p95 8.0 s. Password hashing is the cost (creating the 25 accounts in the container also took minutes). |
| **B. 25 users browsing non-stop** (queue, queue pages, shipment pages, dashboard, My work, documents, search, Savings; no pause between clicks), 500 requests | **5.4 requests/s.** p50 3.0–4.5 s and p95 5–6 s for every page type; search p95 22.5 s; every page type had a maximum of 22.7–24.1 s. No server errors. |
| **Single user, same stack, same pages** | 0.04–0.82 s (median 0.26 s): so the times in B are queueing, not slow pages. |
| **C. Approvals at once**: 5 approvers each approving 10 ready shipments, plus two of them racing on the same 10 (70 requests) | 4.6 s, p50 0.44 s; **every shipment approved exactly once** (60 shipments, 60 approval rows, 60 `shipment.approved` audit events, never two per shipment). The QA-080/081 row locks hold on the production stack. |

## 3. New issue

**QA-105 — Medium — The production server runs a fixed 3 sync workers, so 25 active users queue for 3–5 seconds per page**
`docker-compose.prod.yml` (and the Dockerfile) start `gunicorn --workers 3` (sync worker class) whatever the machine. The Docker VM here had 8 CPUs; with 25 users clicking without pause the stack served 5.4 requests/s, against about 11.5 requests/s that three workers could serve if each request took its single-user time (0.26 s mean) — the rest is CPU contention inside the VM and requests waiting in gunicorn's queue. Every page then takes 3–4.5 s, with stalls of 23 s. Real users pause between clicks (25 users at one click per 15 s is under 2 requests/s), so a team of this size is fine today, but there is no headroom: one person downloading the 5.8 s documents Excel or the 4.5 s journal holds a third of the server, and two such downloads leave one worker for everyone else. Make the worker count configurable (`GUNICORN_WORKERS`, default `2 × CPUs + 1`) or use `--worker-class gthread --threads 4`, and consider moving the big exports to the background worker.

## 4. Notes (not issues)

- Approvals are fast under load (p50 0.44 s) because they are short writes; the row lock adds no visible delay.
- Sign-in at 4–8 s under a burst is the price of the password hasher on this VM; it is per sign-in, not per page.
- Memory during the test: web (3 workers) 256 MB, worker 188 MB, beat 94 MB, Postgres 59 MB, Redis 5 MB, Caddy 11 MB.

## 5. Still untested

A Linux server instead of Docker Desktop (timings here are pessimistic for CPU), more than 25 users, the big exports while others browse, memory of the 6 MB page in a browser, much larger data (30,000+ shipments).

## 6. Test data left behind

As session 19 (the `qa19` stack holds the data set and the 25 `loadNN` users; 60 synthetic shipments are now approved). In the session 18 Postgres container (`shipmatch-qa-pg`, stopped): the same 3,000-shipment data set.
