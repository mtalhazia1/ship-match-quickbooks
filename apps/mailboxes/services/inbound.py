"""Forwarding addresses: <org-slug>-<token>@INBOUND_EMAIL_DOMAIN, delivered by Postmark or Mailgun webhooks.

The random token makes an address unguessable, so knowing an organization's slug is not enough to send it
documents. Regenerating the address invalidates the old one at once.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from email.utils import getaddresses

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.mailboxes.models import Mailbox

from .intake import IncomingAttachment, IncomingEmail, content_hash_key, message_key
from .mime import parse_date

MAILGUN_MAX_AGE_SECONDS = 15 * 60


# ---------------------------------------------------------------- addresses


def _local_part(org) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", (org.slug or "org").lower()).strip("-")[:30].strip("-") or "org"
    token = base64.b32encode(secrets.token_bytes(10)).decode().lower()   # 16 characters, 80 random bits
    return f"{slug}-{token}"


def ensure_inbound(org, actor=None) -> Mailbox:
    """The organization's forwarding-address mailbox, created on first use."""
    mailbox = Mailbox.objects.filter(organization=org, kind=Mailbox.Kind.INBOUND).first()
    if mailbox:
        return mailbox
    for _ in range(5):
        try:
            with transaction.atomic():
                return Mailbox.objects.create(organization=org, kind=Mailbox.Kind.INBOUND,
                                              display_name="Forwarding address", inbound_local=_local_part(org),
                                              created_by=actor if getattr(actor, "is_authenticated", False) else None)
        except IntegrityError:   # another request created it, or (astronomically unlikely) a token clash
            mailbox = Mailbox.objects.filter(organization=org, kind=Mailbox.Kind.INBOUND).first()
            if mailbox:
                return mailbox
    raise RuntimeError("Couldn't create a forwarding address")


def regenerate(mailbox: Mailbox) -> Mailbox:
    old = mailbox.inbound_local
    for _ in range(5):
        mailbox.inbound_local = _local_part(mailbox.organization)
        try:
            with transaction.atomic():
                mailbox.save(update_fields=["inbound_local", "updated_at"])
            return mailbox
        except IntegrityError:
            mailbox.inbound_local = old
    raise RuntimeError("Couldn't create a new forwarding address")


def local_part_of(address: str) -> str | None:
    """The routing key of a recipient address on our inbound domain, or None for other domains."""
    address = (address or "").strip().strip("<>").lower()
    if "@" not in address:
        return None
    local, domain = address.rsplit("@", 1)
    if settings.INBOUND_EMAIL_DOMAIN and domain != settings.INBOUND_EMAIL_DOMAIN:
        return None
    return local.split("+", 1)[0] or None


def route(recipients) -> tuple[Mailbox | None, str]:
    """The enabled forwarding mailbox for the first recipient that is one of ours, and that recipient."""
    seen = set()
    for addr in recipients:
        local = local_part_of(addr)
        if not local or local in seen:
            continue
        seen.add(local)
        mailbox = (Mailbox.objects.select_related("organization")
                   .filter(kind=Mailbox.Kind.INBOUND, inbound_local=local, enabled=True).first())
        if mailbox:
            return mailbox, addr.strip().lower()
    return None, ""


# ---------------------------------------------------------------- Postmark


def postmark_auth_ok(header: str) -> bool:
    user, password = settings.POSTMARK_INBOUND_USER, settings.POSTMARK_INBOUND_PASSWORD
    if not (user and password) or not header.lower().startswith("basic "):
        return False
    try:
        given = base64.b64decode(header.split(" ", 1)[1].strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    given_user, _, given_password = given.partition(":")
    ok_user = hmac.compare_digest(given_user.encode(), user.encode())
    ok_password = hmac.compare_digest(given_password.encode(), password.encode())
    return ok_user and ok_password


def postmark_recipients(payload: dict) -> list[str]:
    out = [payload.get("OriginalRecipient") or ""]
    for key in ("ToFull", "CcFull", "BccFull"):
        out += [(r or {}).get("Email") or "" for r in (payload.get(key) or []) if isinstance(r, dict)]
    for key in ("To", "Cc", "Bcc"):
        out += [a for _, a in getaddresses([payload.get(key) or ""])]
    return [a for a in out if a]


def parse_postmark(payload: dict, recipient: str = "") -> IncomingEmail:
    headers = {}
    for h in payload.get("Headers") or []:
        if isinstance(h, dict) and h.get("Name"):
            headers.setdefault(str(h["Name"]).lower(), str(h.get("Value") or ""))
    full = payload.get("FromFull") or {}
    sender = payload.get("From") or ""
    if isinstance(full, dict) and full.get("Email"):
        sender = f"{full.get('Name') or ''} <{full['Email']}>".strip()
    attachments = []
    for a in payload.get("Attachments") or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("Name") or "")
        try:
            content = base64.b64decode(a.get("Content") or "", validate=False)
            skip = ""
        except (binascii.Error, ValueError):
            content, skip = None, "Couldn't decode this attachment"
        attachments.append(IncomingAttachment(
            filename=name, content_type=str(a.get("ContentType") or "application/octet-stream"),
            content=content, inline=bool(a.get("ContentID")), skip_reason=skip))
    provider_id = str(payload.get("MessageID") or "")
    return IncomingEmail(
        message_id=message_key(headers.get("message-id"), "postmark", provider_id),
        subject=str(payload.get("Subject") or "")[:500], sender=sender[:320],
        received_at=parse_date(str(payload.get("Date") or headers.get("date") or "")),
        recipient=recipient, provider_ref=f"Postmark {provider_id}"[:255] if provider_id else "",
        attachments=attachments, text=str(payload.get("TextBody") or "")[:4000])


# ---------------------------------------------------------------- Mailgun


def mailgun_signature_ok(timestamp: str, token: str, signature: str) -> bool:
    key = settings.MAILGUN_SIGNING_KEY
    if not (key and timestamp and token and signature):
        return False
    expected = hmac.new(key.encode(), f"{timestamp}{token}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def mailgun_fresh(timestamp: str, now: float | None = None) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    return abs((now or time.time()) - ts) <= MAILGUN_MAX_AGE_SECONDS


def mailgun_recipients(post) -> list[str]:
    out = [a for _, a in getaddresses([post.get("recipient") or ""])]
    for key in ("To", "Cc"):
        out += [a for _, a in getaddresses([post.get(key) or ""])]
    return [a for a in out if a]


def _json(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def parse_mailgun(post, files, recipient: str = "") -> IncomingEmail:
    headers = {}
    for pair in _json(post.get("message-headers"), []):
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            headers.setdefault(str(pair[0]).lower(), str(pair[1]))
    cid_map = _json(post.get("content-id-map"), {})
    inline_fields = set(cid_map.values()) if isinstance(cid_map, dict) else set()
    try:
        count = int(post.get("attachment-count") or 0)
    except ValueError:
        count = 0
    names = [f"attachment-{i}" for i in range(1, count + 1)]
    names += sorted((k for k in files if k not in names), key=lambda k: (len(k), k))
    attachments = []
    for field in names:
        f = files.get(field)
        if f is None:
            continue
        content = f.read()
        attachments.append(IncomingAttachment(
            filename=f.name or "", content_type=(getattr(f, "content_type", "") or "application/octet-stream"),
            content=content, inline=field in inline_fields))
    sender = post.get("from") or post.get("sender") or ""
    subject = post.get("subject") or headers.get("subject") or ""
    raw_id = headers.get("message-id") or post.get("Message-Id") or ""
    return IncomingEmail(
        message_id=message_key(raw_id, "mailgun", content_hash_key(
            sender, subject, headers.get("date"), *(hashlib.sha256(a.content or b"").hexdigest() for a in attachments))),
        subject=subject[:500], sender=sender[:320], received_at=parse_date(headers.get("date") or post.get("Date") or ""),
        recipient=recipient, provider_ref=("Mailgun " + raw_id.strip("<> "))[:255] if raw_id else "",
        attachments=attachments, text=(post.get("body-plain") or "")[:4000])
