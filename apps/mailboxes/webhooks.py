"""Inbound email webhooks: Postmark (JSON, HTTP basic auth) and Mailgun routes (multipart, HMAC signature).

Status codes are chosen so each provider retries only when a retry can help:
  * 200 accepted (also for an email we already have, so retries stop);
  * 401/403 bad credentials or signature (a misconfigured key is fixed by an operator; retries then succeed);
  * Postmark 403 / Mailgun 406 for permanent rejections: unknown or paused address, oversized message,
    stale or replayed Mailgun request. Both providers stop retrying on these codes.
    Unknown and paused addresses get the same reply, so callers can't probe which addresses exist;
  * 429 with Retry-After when an organization or caller sends too much at once;
  * 500 for unexpected errors, so the provider tries again later.
"""
from __future__ import annotations

import json
import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import RequestDataTooBig, TooManyFilesSent
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.core.middleware import client_ip

from .services import inbound, intake

log = logging.getLogger(__name__)

# The code each provider treats as "rejected, do not retry".
NO_RETRY = {"postmark": 403, "mailgun": 406}
GENERIC_REJECTION = "This address does not accept email."


def _max_bytes() -> int:
    return max(1, settings.INBOUND_EMAIL_MAX_MB) * 1024 * 1024


def _reject(provider: str, text: str = GENERIC_REJECTION) -> HttpResponse:
    return HttpResponse(text, status=NO_RETRY[provider], content_type="text/plain")


def _over_limit(bucket: str, limit: int) -> bool:
    if limit <= 0:
        return False
    key = f"inbound-rate:{bucket}:{int(time.time() // 60)}"
    cache.add(key, 0, 70)
    try:
        n = cache.incr(key)
    except ValueError:
        cache.set(key, 1, 70)
        n = 1
    return n > limit


def _slow_down() -> HttpResponse:
    response = HttpResponse("Too many emails at once. Try again in a minute.", status=429, content_type="text/plain")
    response["Retry-After"] = "60"
    return response


def _caller_limited(request, provider: str) -> bool:
    # Per calling IP, before authentication: shared by every organization, so it is generous.
    return _over_limit(f"{provider}:ip:{client_ip(request) or '-'}", settings.INBOUND_EMAIL_RATE_PER_MINUTE * 10)


def _org_limited(mailbox) -> bool:
    return _over_limit(f"org:{mailbox.organization_id}", settings.INBOUND_EMAIL_RATE_PER_MINUTE)


def _multipart(request):
    """Mailgun's form fields (POST data) and attachments (files), limited by INBOUND_EMAIL_MAX_MB.

    Django's own parser caps text fields at DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB). Mailgun sends the HTML
    body twice (body-html and stripped-html), so an ordinary email with a large HTML body would be refused
    and, with the "don't retry" answer Mailgun needs for real rejections, lost. The body is read here
    within the email size limit and parsed as MIME instead.
    """
    from email import message_from_bytes
    from email.policy import HTTP

    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.http import QueryDict
    from django.utils.datastructures import MultiValueDict

    ctype = request.META.get("CONTENT_TYPE", "")
    if not ctype.startswith("multipart/form-data"):
        return request.POST, request.FILES     # url-encoded: small by nature, Django's limits apply
    raw = request.read(_max_bytes() + 1)
    if len(raw) > _max_bytes():
        raise RequestDataTooBig("Message too large.")
    msg = message_from_bytes(b"Content-Type: " + ctype.encode("latin-1") + b"\r\n\r\n" + raw, policy=HTTP)
    if not msg.is_multipart():
        raise ValueError("not multipart")
    post, files = QueryDict(mutable=True), MultiValueDict()
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files.appendlist(name, SimpleUploadedFile(filename, payload,
                                                      part.get_content_type() or "application/octet-stream"))
        else:
            charset = part.get_content_charset() or "utf-8"
            post.appendlist(name, payload.decode(charset, "replace"))
    post._mutable = False
    return post, files


def _too_large(request) -> bool:
    try:
        return int(request.META.get("CONTENT_LENGTH") or 0) > _max_bytes()
    except ValueError:
        return True


def _accepted(result) -> JsonResponse:
    return JsonResponse({"status": "accepted" if result.created else "duplicate"})


@csrf_exempt
@require_POST
def postmark(request):
    if _caller_limited(request, "postmark"):
        return _slow_down()
    if not inbound.postmark_auth_ok(request.META.get("HTTP_AUTHORIZATION", "")):
        if not (settings.POSTMARK_INBOUND_USER and settings.POSTMARK_INBOUND_PASSWORD):
            log.warning("Postmark inbound webhook called but POSTMARK_INBOUND_USER/PASSWORD are not set")
        response = HttpResponse("Authentication required.", status=401, content_type="text/plain")
        response["WWW-Authenticate"] = 'Basic realm="ShipMatch inbound email"'
        return response
    if _too_large(request):
        return _reject("postmark", "Message too large.")
    raw = request.read(_max_bytes() + 1)   # read directly: the payload is larger than Django's form limit
    if len(raw) > _max_bytes():
        return _reject("postmark", "Message too large.")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return HttpResponse("Body is not JSON.", status=400, content_type="text/plain")
    if not isinstance(payload, dict):
        return HttpResponse("Body is not a JSON object.", status=400, content_type="text/plain")

    mailbox, recipient = inbound.route(inbound.postmark_recipients(payload))
    if mailbox is None:
        log.info("Postmark inbound: no enabled forwarding address among the recipients")
        return _reject("postmark")
    if _org_limited(mailbox):
        return _slow_down()
    incoming = inbound.parse_postmark(payload, recipient=recipient)
    return _accepted(intake.receive(mailbox, incoming, process="async"))


@csrf_exempt
@require_POST
def mailgun(request):
    if _caller_limited(request, "mailgun"):
        return _slow_down()
    if _too_large(request):
        return _reject("mailgun", "Message too large.")
    try:
        post, files = _multipart(request)
    except (RequestDataTooBig, TooManyFilesSent, ValueError):
        log.warning("Mailgun inbound: a message over %s MB or with unreadable form data was refused",
                    settings.INBOUND_EMAIL_MAX_MB)
        return _reject("mailgun", "Message too large.")
    timestamp, token = post.get("timestamp", ""), post.get("token", "")
    if not inbound.mailgun_signature_ok(timestamp, token, post.get("signature", "")):
        if not settings.MAILGUN_SIGNING_KEY:
            log.warning("Mailgun inbound webhook called but MAILGUN_SIGNING_KEY is not set")
        return HttpResponse("Invalid signature.", status=403, content_type="text/plain")
    if not inbound.mailgun_fresh(timestamp):
        return _reject("mailgun", "Request is too old.")
    replay_key = f"mailgun-token:{token}"
    if not cache.add(replay_key, 1, inbound.MAILGUN_MAX_AGE_SECONDS * 2):
        return _reject("mailgun", "Request was already received.")
    try:
        mailbox, recipient = inbound.route(inbound.mailgun_recipients(post))
        if mailbox is None:
            log.info("Mailgun inbound: no enabled forwarding address among the recipients")
            return _reject("mailgun")
        if _org_limited(mailbox):
            cache.delete(replay_key)   # let Mailgun's retry through
            return _slow_down()
        incoming = inbound.parse_mailgun(post, files, recipient=recipient)
        return _accepted(intake.receive(mailbox, incoming, process="async"))
    except Exception:
        cache.delete(replay_key)       # nothing was saved; let Mailgun's retry through
        raise
