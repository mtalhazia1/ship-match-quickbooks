"""Send one Delivery to its channel and record what happened.

Retries: HTTP 408, 429 and 5xx, timeouts, connection errors and mail server errors are retried with
exponential backoff (30 s, 1 min, 2 min, 4 min; a Retry-After header is respected, up to an hour),
for at most MAX_ATTEMPTS attempts. Other answers (for example Slack's 404 channel_not_found or 410
channel_is_archived) fail at once, with the reason shown on the Alerts page.
"""
from __future__ import annotations

import email.utils
import logging
from dataclasses import dataclass

import httpx
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from .events import Message
from .models import Channel, Delivery
from .render import email_parts, slack_payload, teams_payload
from .webhooks import WebhookURLError, check_webhook_url

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_BASE = 30
BACKOFF_MAX = 30 * 60
RETRY_AFTER_MAX = 60 * 60
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
RETRY_STATUSES = {408, 425, 429}

# Tests replace this with httpx.MockTransport; None means real network.
_TRANSPORT: httpx.BaseTransport | None = None


@dataclass
class Outcome:
    ok: bool
    retry_in: int | None = None  # seconds, when another attempt is scheduled
    slow: bool = False           # the attempt waited for a timeout or a dead connection


def http_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=False, transport=_TRANSPORT,
                        headers={"User-Agent": f"ShipMatch/{settings.APP_VERSION} alerts"})


def backoff(attempts: int, retry_after: int | None = None) -> int:
    delay = min(BACKOFF_BASE * 2 ** max(0, attempts - 1), BACKOFF_MAX)
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
        when = email.utils.parsedate_to_datetime(raw)
        return max(0, int((when - timezone.now()).total_seconds()))
    except (TypeError, ValueError):
        return None


def _explain(kind: str, response: httpx.Response) -> str:
    body = (response.text or "").strip()[:300]
    hints = {
        400: "The service rejected the message format.",
        401: "The webhook needs sign-in; create a new webhook address.",
        403: "The webhook isn't allowed to post here any more (it may have been turned off).",
        404: "The webhook address no longer exists. Create a new one and paste it here.",
        410: "The channel was archived or deleted. Create a new webhook for an active channel.",
    }
    if 300 <= response.status_code < 400:
        return "The webhook answered with a redirect, which ShipMatch doesn't follow. Copy the webhook address again."
    hint = hints.get(response.status_code, f"The service answered HTTP {response.status_code}.")
    return f"{hint} ({body})" if body else hint


def _post(channel: Channel, msg: Message) -> httpx.Response:
    url = check_webhook_url(channel.kind, channel.webhook_url)
    payload = slack_payload(msg) if channel.kind == Channel.Kind.SLACK else teams_payload(msg)
    with http_client() as client:
        return client.post(url, json=payload)


def _email(channel: Channel, msg: Message) -> int:
    if not channel.recipients:
        raise ValueError("The channel has no email addresses.")
    subject, text, html = email_parts(msg)
    mail = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, to=channel.recipients[:50])
    mail.attach_alternative(html, "text/html")
    return mail.send(fail_silently=False)


def _finish(d: Delivery, status: str, *, code: int | None = None, error: str = "", retry_in: int | None = None):
    d.status, d.http_status, d.error = status, code, error[:2000]
    d.next_attempt_at = timezone.now() + timezone.timedelta(seconds=retry_in) if retry_in is not None else None
    if status == Delivery.Status.SENT:
        d.sent_at = timezone.now()
    d.save(update_fields=["status", "http_status", "error", "next_attempt_at", "sent_at", "attempts", "updated_at"])


def attempt(d: Delivery, allow_retry: bool = True) -> Outcome:
    """One try. Never raises: every failure is recorded on the delivery."""
    channel = d.channel
    d.attempts += 1
    if not channel.enabled and not d.is_test:
        _finish(d, Delivery.Status.FAILED, error="The channel was turned off before this message was sent.")
        return Outcome(False)
    msg = Message.from_dict(d.message)
    from apps.demo.mail import outbound_blocked

    if outbound_blocked():
        _finish(d, Delivery.Status.FAILED, error="Not sent: this is the public demo, which never sends messages "
                                                 "outside ShipMatch.")
        return Outcome(False)

    def retry_or_fail(code: int | None, error: str, retry_after: int | None = None, slow: bool = False) -> Outcome:
        if allow_retry and d.attempts < MAX_ATTEMPTS:
            wait = backoff(d.attempts, retry_after)
            _finish(d, Delivery.Status.RETRYING, code=code, error=error, retry_in=wait)
            return Outcome(False, wait, slow)
        _finish(d, Delivery.Status.FAILED, code=code,
                error=error + (f" Gave up after {d.attempts} attempts." if d.attempts > 1 else ""))
        return Outcome(False)

    try:
        if channel.kind == Channel.Kind.EMAIL:
            _email(channel, msg)
            _finish(d, Delivery.Status.SENT)
            return Outcome(True)
        response = _post(channel, msg)
    except WebhookURLError as e:
        _finish(d, Delivery.Status.FAILED, error=str(e))
        return Outcome(False)
    except httpx.TimeoutException:
        return retry_or_fail(None, "The service didn't answer in time.", slow=True)
    except httpx.TransportError as e:
        return retry_or_fail(None, f"Couldn't connect ({type(e).__name__}).", slow=True)
    except (OSError, ConnectionError) as e:  # SMTP server down or refusing
        return retry_or_fail(None, f"The mail server didn't accept the message ({type(e).__name__}: {e}).", slow=True)
    except Exception as e:  # bad address list, template error: retrying won't help
        log.exception("Alert delivery %s failed", d.pk)
        _finish(d, Delivery.Status.FAILED, error=f"{type(e).__name__}: {e}")
        return Outcome(False)

    code = response.status_code
    if 200 <= code < 300:
        _finish(d, Delivery.Status.SENT, code=code)
        return Outcome(True)
    if code in RETRY_STATUSES or code >= 500:
        busy = "The service is busy (rate limited)." if code == 429 else f"The service had a problem (HTTP {code})."
        return retry_or_fail(code, busy, _retry_after(response))
    _finish(d, Delivery.Status.FAILED, code=code, error=_explain(channel.kind, response))
    return Outcome(False)
