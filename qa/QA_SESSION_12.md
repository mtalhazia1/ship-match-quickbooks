# ShipMatch QA — Session 12: PostgreSQL (8 Oct 2026)

First session on the production database engine. A throwaway `postgres:16` container (the image the project's `docker-compose.yml` uses) on `127.0.0.1:55432`, with the app pointed at it through `DATABASE_URL`; the SQLite development database was not touched. Same rules: **issues listed first**; the fixes below were made afterwards at the user's request. Issue ids continue from session 11 (last was QA-082). Branch under test: `fix/qa-session-08`.

## 1. Coverage this session

| Area | Status | Result |
|---|---|---|
| **Migrations on an empty Postgres database** | ✅ | Every migration applies cleanly. |
| **Demo build on Postgres (`reset_demo`)** | ✅ | 53 documents read, matched to 20 shipments, rate checks, month-end data and a vendor statement imported (2 min 10 s). |
| **Whole automated test suite on Postgres** | ✅ | 1,063 passed, 2 skipped, 1 failed. The failure (`test_inbound_rate_limit_asks_the_provider_to_retry`) passes alone on Postgres 3 times out of 3: its two requests straddled a one-minute boundary of the fixed-window counter, so it is test flakiness, not a database difference. See the fixes below. |
| **Row-lock races on real Postgres (QA-080/081 re-run)** | ✅ | Four simultaneous approvals of one shipment: one approval, one audit event. Approve, reject and approve at once on another shipment: one approval, the reject refused. No deadlocks, no 500s. |
| **Every form submitted with 1,000-character text (SQLite does not enforce column lengths)** | ✅ | 411 distinct forms across 261 pages. One failure: QA-083. |
| **Every form with a NUL character, with 40-digit numbers, with 3,000,000,000** | ✅ | Three more runs of the same crawl. One more failure class: QA-084. NUL characters, giant numbers and over-range integers are otherwise handled everywhere. |

## 2. New issues

**QA-083 — Medium on Postgres — The dispute settings page crashes (HTTP 500) when the reply-to address is longer than 254 characters**
`disputes.views.dispute_settings` checks that the address looks like an email but not that it fits the 254-character column. SQLite stores any length; Postgres refuses it (`value too long for type character varying(254)`). The same helper (`clean_emails`) is used for the vendor address and the cc box on dispute drafts. Also, an invalid address was echoed back in full in the error message.

**QA-084 — Low/Medium — Moving a document into a shipment that someone else is emptying at the same moment gives HTTP 500, and the emptied shipment can be deleted just after a document landed in it**
Reproduced by running four copies of the form crawl at once (24 failures, all on the "Wrong shipment?" move form): `Save with update_fields did not affect any rows` raised from `validate_shipment` after the target shipment had been deleted by the request that moved its last document away. The cleanup in `assign_manually` also checks "is the old shipment empty?" and deletes it in two separate steps, so a document moved in between would lose its match.

## 3. Fixed afterwards (same day)

- **QA-083:** `clean_emails` (`apps/disputes/services/workflow.py`) now refuses an address over 254 characters with "…is too long for an email address", and shows at most 60 characters of any bad address in its messages.
- **QA-084:** `assign_manually` (`apps/shipments/services/matching.py`) now runs in one transaction that locks both shipments in id order, and raises `ShipmentGone` if the target was removed. The move view (`apps/shipments/views.py`) shows "That shipment was removed a moment ago. Choose another one." and, if the shipment vanishes while it is being checked, "This document was moved by someone else at the same moment."
- **Test flakiness:** the inbound-rate-limit test (`tests/test_email_inbound.py`) now freezes the clock it counts minutes with.
- **Tests:** 4 new tests in `tests/test_qa_remaining.py` (long reply-to, move into a removed shipment, view message, shipment vanishing during the check), all passing on SQLite and Postgres.
- **Re-run:** the same four crawls started at the same moment as before now give **0 server errors each** (before: 1, 4, 9 and 11 failures).

## 4. Notes (not issues)

- The "flash" messages on a response can belong to another request when several run at once, because they travel in the shared session. Harmless.
- Several old `runserver` processes from earlier days (ports 8000 and others) are still running on this machine; they are not part of this session.

## 5. Still untested

Real webhook delivery and replay (needs a public receiver); real IMAP / Microsoft / Gmail mailboxes; QuickBooks disconnect / reconnect and Xero connect; other browsers and a screen reader; posting to QuickBooks twice at once; Redis and Celery workers (the Docker stack's other services); sort order of empty values (Postgres puts them last, SQLite first).

## 6. Test data left behind

The Postgres container `shipmatch-qa-pg` (databases `shipmatch_qa` and `test_shipmatch_qa`) with the demo data and the crawl's changes. It is throwaway and can be removed with `docker rm -f shipmatch-qa-pg`. The SQLite database was not used.
