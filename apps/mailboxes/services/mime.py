"""Turn a raw RFC 5322 email into an IncomingEmail with the stdlib `email` package.

Handles nested multiparts (mixed > alternative > related), forwarded emails attached as message/rfc822,
base64 and quoted-printable bodies, RFC 2047 encoded subjects and RFC 2231 encoded or continued filenames.
"""
from __future__ import annotations

import base64
import email
import email.policy
import logging
from email.message import Message
from email.utils import parsedate_to_datetime

from django.utils import timezone

from .intake import IncomingAttachment, IncomingEmail, message_key

log = logging.getLogger(__name__)
BODY_TYPES = {"text/plain", "text/html"}


def _header(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:  # malformed header: fall back to the raw text
        value = next((v for k, v in msg.raw_items() if k.lower() == name.lower()), "")
    return " ".join(str(value or "").split())


def parse_date(value: str):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else timezone.make_aware(dt, timezone.utc)


def _filename(part: Message) -> str:
    try:
        name = part.get_filename() or ""
    except Exception:
        name = ""
    return " ".join(str(name).replace("\\", "/").split("/")[-1].split()).strip()


def _payload(part: Message) -> bytes | None:
    try:
        data = part.get_payload(decode=True)
    except Exception:
        log.warning("Couldn't decode an attachment", exc_info=True)
        return None
    if data is None:
        payload = part.get_payload()
        return payload.encode("utf-8", "replace") if isinstance(payload, str) else None
    return data


def attachments_of(msg: Message) -> list[IncomingAttachment]:
    out = []
    for part in msg.walk():
        if part.is_multipart():   # containers, including message/rfc822 (walk() descends into it)
            continue
        ctype = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        name = _filename(part)
        if not name and disposition != "attachment" and (ctype in BODY_TYPES or ctype.startswith("text/")):
            continue   # a body part, not an attachment
        content = _payload(part)
        inline = disposition == "inline" or (disposition == "" and bool(part.get("Content-ID")))
        out.append(IncomingAttachment(filename=name, content_type=ctype, content=content, inline=inline,
                                      skip_reason="" if content is not None else "Couldn't decode this attachment"))
    return out


def body_text(msg: Message, limit: int = 4000) -> str:
    try:
        part = msg.get_body(preferencelist=("plain",)) if hasattr(msg, "get_body") else None
        return (part.get_content() if part else "")[:limit]
    except Exception:
        return ""


def parse_message(raw: bytes, *fallback_key) -> IncomingEmail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    subject = _header(msg, "Subject")
    sender = _header(msg, "From")
    date = _header(msg, "Date")
    return IncomingEmail(
        message_id=message_key(_header(msg, "Message-ID"), *fallback_key),
        subject=subject[:500], sender=sender[:320], received_at=parse_date(date),
        recipient=_header(msg, "Delivered-To") or _header(msg, "To"),
        attachments=attachments_of(msg), text=body_text(msg))


# ---------------------------------------------------------------- IMAP modified UTF-7 (RFC 3501 5.1.3)


def imap_utf7_encode(name: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append(f"&{b64}-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(name: str) -> str:
    out, i = [], 0
    while i < len(name):
        ch = name[i]
        if ch == "&":
            end = name.find("-", i)
            if end == -1:
                out.append(name[i:])
                break
            chunk = name[i + 1:end]
            if not chunk:
                out.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                try:
                    out.append(base64.b64decode(b64).decode("utf-16-be"))
                except ValueError:
                    out.append(name[i:end + 1])
            i = end + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def imap_quote(name: str) -> str:
    """A folder name as an IMAP astring: modified UTF-7, quoted when needed."""
    encoded = imap_utf7_encode(name)
    if encoded and all(c.isalnum() or c in "-_./&" for c in encoded):
        return encoded
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'
