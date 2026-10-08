"""Send one webhook delivery, record the answer, and decide about retries and turning the endpoint off.

* 2xx is success. Anything else (including redirects, which are never followed), timeouts and connection
  errors are retried with exponential backoff: 30 s, 2 min, 10 min, 30 min, 1 h, 3 h, 6 h (Retry-After is
  respected up to an hour) for at most WEBHOOK_MAX_ATTEMPTS attempts.
* An address that now resolves to a private network fails at once.
* After WEBHOOK_DISABLE_AFTER_FAILURES failed attempts in a row (across deliveries) the endpoint is turned
  off, its waiting deliveries are stopped and the admins get an email. One success resets the count.
* Test events don't count towards turning an endpoint off.
"""
from __future__ import annotations

import email.utils
import json
import logging
import time
from dataclasses import dataclass

import httpx
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import signing, urlguard
from .models import WebhookDelivery, WebhookEndpoint

log = logging.getLogger(__name__)

BACKOFF = [30, 120, 600, 1800, 3600, 3 * 3600, 6 * 3600]
RETRY_AFTER_MAX = 3600
BODY_EXCERPT_BYTES = 2048
BODY_EXCERPT_CHARS = 1000

# Tests replace this with httpx.MockTransport; None means the real network.
_TRANSPORT: httpx.BaseTransport | None = None


@dataclass
class Outcome:
    ok: bool
    retry_in: int | None = None


def backoff(attempts: int, retry_after: int | None = None) -> int:
    delay = BACKOFF[min(max(attempts, 1), len(BACKOFF)) - 1]
    if retry_after:
        delay = max(delay, min(retry_after, RETRY_AFTER_MAX))
    return int(delay)


def _retry_after(response: httpx.Response) -> int | None:
    raw = (response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    try:
        return max(0, int((email.utils.parsedate_to_datetime(raw) - timezone.now()).total_seconds()))
    except (TypeError, ValueError):
        return None


def body_bytes(delivery: WebhookDelivery) -> bytes:
    return json.dumps(delivery.event.payload, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def headers_for(delivery: WebhookDelivery, body: bytes, timestamp: int) -> dict:
    endpoint = delivery.endpoint
    return {
        "Content-Type": "application/json",
        "User-Agent": f"ShipMatch-Webhooks/{settings.APP_VERSION}",
        "ShipMatch-Event-Id": delivery.event.event_id,
        "ShipMatch-Event-Type": delivery.event.type,
        "ShipMatch-Delivery-Id": str(delivery.pk),
        signing.TIMESTAMP_HEADER: str(timestamp),
        signing.SIGNATURE_HEADER: signing.signature_header(endpoint.signing_secrets(), timestamp, body),
    }


def _excerpt(raw: bytes) -> str:
    text = raw[:BODY_EXCERPT_BYTES].decode("utf-8", errors="replace")
    text = "".join(c if c.isprintable() or c in "\n\t" else " " for c in text)
    return text.strip()[:BODY_EXCERPT_CHARS]


def _post(delivery: WebhookDelivery) -> tuple[int, str, int]:
    """Returns (status code, body excerpt, milliseconds)."""
    target = urlguard.check_and_resolve(delivery.endpoint.url)
    body = body_bytes(delivery)
    headers = headers_for(delivery, body, int(time.time()))
    headers["Host"] = target.host_header
    timeout = httpx.Timeout(settings.WEBHOOK_TIMEOUT_SECONDS, connect=min(5.0, settings.WEBHOOK_TIMEOUT_SECONDS))
    started = time.monotonic()
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=_TRANSPORT, trust_env=False) as client:
        with client.stream("POST", target.url, content=body, headers=headers,
                           extensions={"sni_hostname": target.sni_host}) as response:
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= BODY_EXCERPT_BYTES:
                    break
            code = response.status_code
            delivery._retry_after = _retry_after(response)
    return code, _excerpt(b"".join(chunks)), int((time.monotonic() - started) * 1000)


def _finish(d: WebhookDelivery, status: str, *, code=None, body: str = "", error: str = "", ms=None,
            retry_in: int | None = None) -> None:
    d.status, d.response_status, d.response_body, d.error, d.duration_ms = status, code, body, error[:500], ms
    d.next_attempt_at = timezone.now() + timezone.timedelta(seconds=retry_in) if retry_in is not None else None
    if status == WebhookDelivery.Status.SUCCEEDED:
        d.delivered_at = timezone.now()
    d.save(update_fields=["status", "response_status", "response_body", "error", "duration_ms", "next_attempt_at",
                          "delivered_at", "attempts", "updated_at"])


def attempt(d: WebhookDelivery, allow_retry: bool = True) -> Outcome:
    """One try. Never raises: every failure is recorded on the delivery."""
    from apps.demo.mail import outbound_blocked

    endpoint = d.endpoint
    d.attempts += 1
    if outbound_blocked():
        _finish(d, WebhookDelivery.Status.FAILED,
                error="Not sent: this is the public demo, which never sends anything outside ShipMatch.")
        return Outcome(False)
    if not endpoint.enabled and not d.is_test:
        _finish(d, WebhookDelivery.Status.FAILED, error="The endpoint was turned off before this event was sent.")
        return Outcome(False)

    def failed(error: str, code=None, body="", ms=None, retry=True, retry_after=None) -> Outcome:
        if not d.is_test:
            _count_failure(endpoint)
        if retry and allow_retry and not d.is_test and d.attempts < settings.WEBHOOK_MAX_ATTEMPTS:
            endpoint.refresh_from_db(fields=["enabled"])
            if endpoint.enabled:
                wait = backoff(d.attempts, retry_after)
                _finish(d, WebhookDelivery.Status.RETRYING, code=code, body=body, error=error, ms=ms, retry_in=wait)
                return Outcome(False, wait)
        suffix = f" Gave up after {d.attempts} attempts." if d.attempts > 1 else ""
        _finish(d, WebhookDelivery.Status.FAILED, code=code, body=body, error=error + suffix, ms=ms)
        return Outcome(False)

    try:
        code, body, ms = _post(d)
    except urlguard.URLRejected as e:
        return failed(str(e), retry=not e.permanent)
    except httpx.TimeoutException:
        return failed(f"No answer within {settings.WEBHOOK_TIMEOUT_SECONDS:g} seconds.")
    except httpx.TransportError as e:
        return failed(f"Couldn't connect ({type(e).__name__}).")
    except Exception as e:  # pragma: no cover - unexpected; recorded, not retried
        log.exception("Webhook delivery %s failed", d.pk)
        return failed(f"{type(e).__name__}: {e}"[:300], retry=False)

    if 200 <= code < 300:
        _finish(d, WebhookDelivery.Status.SUCCEEDED, code=code, body=body, ms=ms)
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(consecutive_failures=0, last_success_at=timezone.now())
        return Outcome(True)
    if 300 <= code < 400:
        error = f"The endpoint answered with a redirect (HTTP {code}), which ShipMatch doesn't follow. Use the final address."
    else:
        error = f"The endpoint answered HTTP {code}."
    return failed(error, code=code, body=body, ms=ms, retry_after=getattr(d, "_retry_after", None))


def _count_failure(endpoint: WebhookEndpoint) -> None:
    now = timezone.now()
    WebhookEndpoint.objects.filter(pk=endpoint.pk).update(consecutive_failures=F("consecutive_failures") + 1,
                                                          last_failure_at=now)
    endpoint.refresh_from_db(fields=["consecutive_failures", "enabled"])
    limit = settings.WEBHOOK_DISABLE_AFTER_FAILURES
    if endpoint.enabled and limit and endpoint.consecutive_failures >= limit:
        disable(endpoint, f"Turned off after {endpoint.consecutive_failures} failed attempts in a row.")


def disable(endpoint: WebhookEndpoint, reason: str) -> bool:
    """Turn the endpoint off (once), stop its waiting deliveries and tell the admins."""
    with transaction.atomic():
        changed = WebhookEndpoint.objects.filter(pk=endpoint.pk, enabled=True).update(
            enabled=False, disabled_at=timezone.now(), disabled_reason=reason[:300])
        if not changed:
            return False
        WebhookDelivery.objects.filter(endpoint_id=endpoint.pk, status__in=[
            WebhookDelivery.Status.PENDING, WebhookDelivery.Status.RETRYING]).update(
            status=WebhookDelivery.Status.FAILED, next_attempt_at=None,
            error="Not sent: the endpoint was turned off after repeated failures.")
        endpoint.refresh_from_db()
        from apps.core.utils import audit

        audit(endpoint.organization, "webhook.disabled", endpoint, host=endpoint.host, reason=reason)
        org = endpoint.organization
        host, url = endpoint.host, endpoint.display_url

        def tell_admins():
            from django.core.mail import send_mail
            from django.urls import reverse

            from apps.billing.emails import admin_emails

            try:
                to = admin_emails(org)
                if to:
                    link = f"{settings.SITE_URL}{reverse('integrations:webhook_edit', args=[endpoint.pk])}"
                    send_mail(f"Webhook to {host} turned off ({org.name})",
                              f"ShipMatch stopped sending events to {url} for {org.name}: {reason}\n\n"
                              "Check that the receiving system is up and answers with HTTP 2xx, then turn the "
                              f"endpoint on again and replay the events it missed:\n{link}\n",
                              None, to, fail_silently=False)
            except Exception:
                log.exception("Could not email admins about disabled webhook %s", endpoint.pk)

        transaction.on_commit(tell_admins)
    return True
