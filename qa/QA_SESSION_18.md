# ShipMatch QA — Session 18: background workers (16 Oct 2026)

Same rules: **issues listed, nothing fixed**. First session with real background workers: a Celery worker (`--pool=threads`, 2 and then 4 threads; prefork does not run on Windows) and Celery beat against Redis, the app on `runserver 127.0.0.1:8002`, `CELERY_TASK_ALWAYS_EAGER=0`. Database: a throwaway PostgreSQL 16 container (`shipmatch-qa-pg`, `127.0.0.1:25432`) built with `reset_demo --force`; broker and cache: a throwaway `redis:7-alpine` container (`shipmatch-qa-redis`, `127.0.0.1:56379`, default config: RDB snapshots, no append-only file). The other project's containers on 5432/6379/8000 were left alone. `db.sqlite3` was not used. Email on the console backend.
Branch under test: `merge/qa-sessions-08-13` at `b94b52f`. Issue ids continue from QA-090.

## 1. Coverage this session

| Area | Status | Result |
|---|---|---|
| Upload read by the worker (API and web, PDF and ZIP) | ✅ | Received → matched in 2–3 s; a ZIP of 6 PDFs queued all 6 children, all matched. |
| Upload while no worker runs | ✅ | Task waits in the queue; the page says "Being read now. Refresh in a few seconds." Read as soon as a worker starts. |
| Worker killed while reading (hard kill of the process tree) | 🟡 | Queued tasks are read after restart; the two in progress are not — QA-092. |
| Same document task run twice (redelivery) | ✅ | Identical fields, issues, links and counts; a reviewer's correction (`source=human`) survives a re-run. |
| 52 documents through 4 worker threads at once | ✅ | Every document in the right shipment (checked against the generator's ground truth): none split, none merged, no deadlocks, no retries. 3 min 40 s in all (≈4.2 s per document; per task 13 s median, as matching is serialized per organization by design). Thread-pool timing on Windows says little about prefork on Linux: see session 20. |
| Redis stopped (broker and cache) | ✅ | QA-091, 094, 095. `/health/` 200, `/health/ready/` 503 "cache: ConnectionError" (correct). Pages for signed-in users keep working. |
| Redis crashed with tasks queued (`docker kill`) | ✅ | Both queued tasks lost — QA-093. |
| Worker reconnects after a Redis outage | ✅ | Retries with growing pauses ("Trying again in 16 s… (8/100)"), then resumes. |
| Every beat task once through the worker | ✅ | All 10 succeed (`poll_all_mailboxes`, `flag_overdue_disputes`, `send_daily_digests`, `retry_stalled_deliveries`, `check_free_time`, `purge_try_uploads`, `retry_due_deliveries`, `purge_pending_signups`, `sync_all_payments`); beat itself fired the minute and hourly tasks on schedule. |
| Daily summary | ✅ | Sent once by email (console) even when triggered twice at the same moment: the "claim the day" update works. |
| Alert email with the mail server down | ✅ | Retried with back-off (30 s, 60 s…), error text shown per delivery. Worker killed while a retry was scheduled: the 10-minute sweeper (`retry_stalled_deliveries`) re-sends it after 15 minutes; a late duplicate task is "skipped". |
| Assignment email | ✅ | Queued after commit, sent by the worker within a second. |
| Posting and payment check with Redis down | ✅ | QA-095 (a fake QuickBooks connection was created for this and deleted before Redis came back; nothing reached Intuit). |
| Tasks for rows deleted while queued | ✅ | QA-096. |
| Demo reset through the worker | ⬜ | Not run. Code check: every step reads documents inline (`process="sync"`), so a worker run builds the whole demo before it ends. |

## 2. New issues

**QA-091 — Medium/High — A document uploaded while the broker is unreachable is stored but never read, and can't be uploaded again**
With Redis down, a web upload waits about 11 s and returns HTTP 500 (`kombu OperationalError` from `process_document_task.delay` in the `on_commit` hook of `ingest_bytes`). The document row and file are already committed (doc 89, status "received"). After Redis is back nothing picks it up: there is no sweep for documents left in "received" (alerts and webhooks have one). Its page says "It is being read now. Refresh in a few seconds." indefinitely, and uploading the same file again says "was uploaded before, so it was not added again", so the user has no way to retry. The same end state follows from QA-093. Suggested: queue defensively (catch the error, keep the document, say "will be read shortly"), add a beat sweep that re-queues documents in "received" for more than a few minutes, and let a duplicate upload of an unread document re-queue it.

**QA-092 — Medium — A document whose worker died mid-read stays "Extracted" for at least an hour, with a misleading message**
Killing the worker while two documents were being read left them "Extracted"; the queued ones were read after restart, these two were not. Their tasks sit in Redis's `unacked` set until the visibility timeout (kombu default 1 hour, not set in `config/settings.py`). They were read only when a worker **started** after the timeout had passed (21:22, due 21:16): a worker that keeps running did not restore them in the six minutes past due. Meanwhile the document page says "No bill of lading, container or PO number was found that links it to a shipment", which is false — matching never ran. Suggested: the same sweep as QA-091 for "extracted" documents older than a few minutes, a shorter `broker_transport_options={"visibility_timeout": …}` matched to the longest task, and no "nothing found" message for a document that hasn't finished.

**QA-093 — Medium — Production Redis has no persistence: queued work is lost on a crash or a container re-create**
`docker-compose.yml` runs `redis:7` with no volume and no `appendonly`; `docker-compose.prod.yml` adds nothing. A `docker kill` with two document tasks queued lost both (queue length 2 → 0); a `docker compose up` that re-creates the container loses everything, including scheduled alert/webhook retries (those are recovered by their sweepers; documents are not — QA-091). Suggested: a volume and `--appendonly yes` for the Redis service, plus the document sweep.

**QA-094 — Medium — With the cache down, sign-in and every API call return HTTP 500**
`accounts/services/lockout.py` (`is_locked`) and `api._rate_limit` read Redis without a fallback: login → 500 after 2.3 s; `/api/demo/shipments` and API uploads → 500. Signed-in pages keep working, so the outage is invisible to staff already in and total for everyone else and for integrations. Decide per counter whether to fail open (rate limits) or closed with a clear message (lockout), but not with a 500.

**QA-095 — Low — "Post bills" and "Check payments now" return HTTP 500 after 5–11 s when the broker is unreachable**
`post_shipment_task.delay` (`shipments/views.py:577`, `workflow/services/decisions.py:143`) and `sync_org_payments.delay` (`accounting/views.py:371`) are not guarded. Nothing is changed (no audit row, no posting), so trying again later works; it just looks broken. A bulk "Post" would stop at the first shipment. Show "Couldn't start posting; try again in a minute".

**QA-096 — Low — Tasks for a document or shipment deleted while queued fail with an unhandled `DoesNotExist`**
`process_document_task(999999)` and `post_shipment_task(999999)` raise `Document/Shipment.DoesNotExist` ("raised unexpected" in the worker log). This happens for real when a split is undone, a document is removed, or the demo is reset while tasks are queued. The alert, webhook and email tasks already return "skipped" for missing rows; these two should do the same.

## 3. Notes (not issues)

- README says the Docker stack runs "two Celery workers"; it runs one `worker` service with `--concurrency 2`.
- No Celery task time limit is set (`CELERY_TASK_TIME_LIMIT` / `soft_time_limit`): a document that hangs the PDF reader would hold a worker slot indefinitely. To try in session 21 (file-upload abuse).
- Celery runs without a result backend, so `AsyncResult` state can't be read (fine; nothing in the app relies on it).
- The local run used Python 3.14 (the venv); the Docker image is `python:3.12-slim`.

## 4. Still untested

Prefork workers on Linux (the Docker stack) and their throughput; a worker killed during **posting** (needs QuickBooks, session 16); the demo reset through the worker; webhook delivery to a real receiver (session 14); mailbox polling against a real mailbox (session 15).

## 5. Test data left behind

The containers `shipmatch-qa-pg` and `shipmatch-qa-redis` (throwaway; `docker rm -f shipmatch-qa-pg shipmatch-qa-redis`). The Postgres demo build and the uploads stored their files in this working copy's `media/` folder (the same folder `db.sqlite3` uses); none of the 148 files `db.sqlite3` references was changed or removed (checked).
