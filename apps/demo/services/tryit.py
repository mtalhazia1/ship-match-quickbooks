"""The public "Try it on your invoice" page: limits, sandbox processing, results and clean-up.

Every upload is read in its own new sandbox organization, so a visitor's document is never mixed
with a customer's or another visitor's (matching and duplicate checks only see that one file).
Abuse controls: per-IP limits per hour and per day (cache), a global daily cap that bounds AI cost,
size and page limits checked before anything is read. Everything is deleted after TRY_RETENTION_HOURS.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import FileSystemStorage, default_storage
from django.db import transaction
from django.utils import timezone

from apps.core.models import Organization
from apps.core.utils import audit
from apps.demo.models import TrySubmission
from apps.documents.models import Document
from apps.documents.schemas import SCHEMAS
from apps.documents.services.ingest import ingest_bytes, safe_filename
from apps.documents.tasks import process_document_task

log = logging.getLogger(__name__)
SANDBOX_NAME = "Try page upload (deleted automatically)"


class TryRejected(ValueError):
    """The upload can't be accepted; the message is shown to the visitor."""


# --------------------------------------------------------------------------- limits


def _ip_bucket(ip: str | None) -> str:
    """IPv6 visitors are counted per /64 network: one home or server gets a whole /64, so counting single
    addresses would let one visitor rotate around the limit."""
    import ipaddress

    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return ip or "unknown"
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return str(mapped)
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def ip_hash(ip: str | None) -> str:
    """Keyed hash of the visitor's IP: enough to count requests, not enough to recover the address."""
    return hashlib.sha256(f"{settings.SECRET_KEY}|try|{_ip_bucket(ip)}".encode()).hexdigest()[:32]


def _keys(iph: str, now: datetime) -> tuple[str, str]:
    return f"try:ip:{iph}:h:{now:%Y%m%d%H}", f"try:ip:{iph}:d:{now:%Y%m%d}"


def rate_limit_message(iph: str) -> str:
    """'' when this visitor may upload now; otherwise what to tell them."""
    now = timezone.now()
    hour_key, day_key = _keys(iph, now)
    if (cache.get(hour_key) or 0) >= settings.TRY_RATE_PER_HOUR:
        return (f"You've tried {settings.TRY_RATE_PER_HOUR} documents in the last hour, the limit for this page. "
                "Try again in an hour, or contact us for a full trial with your own documents.")
    if (cache.get(day_key) or 0) >= settings.TRY_RATE_PER_DAY:
        return (f"You've tried {settings.TRY_RATE_PER_DAY} documents today, the limit for this page. "
                "Try again tomorrow, or contact us for a full trial with your own documents.")
    return ""


def _incr(key: str, ttl: int) -> int:
    cache.add(key, 0, ttl)
    try:
        return cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, ttl)
        return 1


def count_attempt(iph: str) -> None:
    hour_key, day_key = _keys(iph, timezone.now())
    _incr(hour_key, 3600 + 60)
    _incr(day_key, 86400 + 60)


def _today_start() -> datetime:
    return datetime.combine(timezone.now().astimezone(dt_timezone.utc).date(), time.min, tzinfo=dt_timezone.utc)


def _global_key() -> str:
    return f"try:global:{_today_start():%Y%m%d}"


def used_today() -> int:
    """Uploads accepted today (UTC), from the database and the cache, whichever is higher."""
    in_db = TrySubmission.objects.filter(created_at__gte=_today_start()).count()
    return max(in_db, cache.get(_global_key()) or 0)


def daily_cap_reached() -> bool:
    return used_today() >= settings.TRY_DAILY_LIMIT


# --------------------------------------------------------------------------- upload


def max_bytes() -> int:
    return int(settings.TRY_MAX_MB * 1024 * 1024)


def _page_count(content: bytes) -> int:
    """Pages in the PDF, counted with pypdf: it reads the page tree without laying pages out, so a file
    built with tens of thousands of empty pages is refused in seconds instead of tying up a worker."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("encrypted")
        return len(reader.pages)
    except Exception as e:  # noqa: BLE001 - any parser error means we can't read it
        raise TryRejected("We couldn't open this PDF. It may be damaged or protected with a password. "
                          "Save it again as a PDF and try once more.") from e


def format_mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}".rstrip("0").rstrip(".")


def validate_upload(upload) -> tuple[bytes, int]:
    """Check one uploaded file against the page's limits. Returns (content, page count)."""
    if upload is None:
        raise TryRejected("Choose a PDF to upload.")
    limit = max_bytes()
    if upload.size > limit:
        raise TryRejected(f"This file is {format_mb(upload.size)} MB. The limit here is {format_mb(limit)} MB; "
                          "upload a smaller PDF, for example only the invoice pages.")
    content = upload.read(limit + 1)
    if not content:
        raise TryRejected("That file is empty. Choose the PDF again.")
    if len(content) > limit:
        raise TryRejected(f"This file is larger than {format_mb(limit)} MB. Upload a smaller PDF.")
    if not content.startswith(b"%PDF"):
        raise TryRejected("Only PDF files can be read here. Save or print your invoice as a PDF and try again.")
    pages = _page_count(content)
    if pages == 0:
        raise TryRejected("This PDF has no pages. Choose another file.")
    if pages > settings.TRY_MAX_PAGES:
        raise TryRejected(f"This PDF has {pages} pages. The limit here is {settings.TRY_MAX_PAGES} pages; "
                          "upload only the invoice or bill of lading.")
    return content, pages


def _new_sandbox() -> Organization:
    """A fresh organization for one upload. Never reuses an existing organization."""
    for _ in range(5):
        slug = f"try-{secrets.token_hex(6)}"
        if not Organization.objects.filter(slug=slug).exists():
            return Organization.objects.create(name=SANDBOX_NAME, slug=slug)
    raise RuntimeError("Could not create a sandbox organization")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def submit(upload, iph: str) -> tuple[TrySubmission, str]:
    """Store the upload in a new sandbox and start reading it. Returns (submission, secret token)."""
    content, pages = validate_upload(upload)
    filename = safe_filename(getattr(upload, "name", "") or "document.pdf")
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        org = _new_sandbox()
        sub = TrySubmission.objects.create(
            token_hash=token_hash(token), organization=org, filename=filename, size_bytes=len(content),
            page_count=pages, ip_hash=iph,
            expires_at=timezone.now() + timedelta(hours=settings.TRY_RETENTION_HOURS))
        doc, _ = ingest_bytes(org, filename, content, source=Document.Source.UPLOAD, process="none")
        sub.document = doc
        sub.save(update_fields=["document"])
    _incr(_global_key(), 86400 + 3600)
    # Committed above, so a worker can see it. Celery runs this inline when CELERY_TASK_ALWAYS_EAGER=1.
    process_document_task.delay(doc.pk)
    return sub, token


def find(token: str) -> TrySubmission | None:
    """The submission for a result URL; None if the token is wrong or the result has expired."""
    if not token or len(token) > 100:
        return None
    sub = (TrySubmission.objects.select_related("document", "organization")
           .filter(token_hash=token_hash(token)).first())
    if sub is None or sub.expired:
        return None
    return sub


# --------------------------------------------------------------------------- results


@dataclass
class Row:
    name: str
    value: object
    state: str  # ok | low | missing (same colours as the review screen)


def result_rows(doc: Document) -> list[Row]:
    threshold = doc.organization.review_threshold or settings.REVIEW_CONFIDENCE_THRESHOLD
    fields = {f.name: f for f in doc.fields.all()}
    order = [n for n in (SCHEMAS[doc.doc_type].model_fields if doc.doc_type in SCHEMAS else fields)
             if n != "line_items"]
    order += [n for n in fields if n not in order and n != "line_items"]
    rows = []
    for n in order:
        f = fields.get(n)
        if f is None or f.value in (None, "", []):
            rows.append(Row(n, None, "missing"))
        else:
            rows.append(Row(n, f.value, "ok" if f.confidence >= threshold else "low"))
    return rows


def document_issues(doc: Document) -> list[dict]:
    """The checks ShipMatch runs on a single document (no shipment context), read only."""
    from apps.shipments.labels import issue_guidance, issue_title
    from apps.shipments.services import validation

    if doc.doc_type not in SCHEMAS:
        return []
    data = doc.data()
    threshold = doc.organization.review_threshold or settings.REVIEW_CONFIDENCE_THRESHOLD
    specs = []
    for rule in list(validation.DOCUMENT_RULES):
        try:
            specs.extend(rule(doc, data))
        except Exception:  # noqa: BLE001 - one failing rule must not hide the others
            log.exception("Validation rule %s failed on try page document", getattr(rule, "__name__", rule))
    specs.extend(validation.check_low_confidence(doc, threshold))
    order = {"error": 0, "warning": 1}
    return [{"code": s.code, "severity": s.severity, "title": issue_title(s.code), "message": s.message,
             "guidance": issue_guidance(s.code), "amount_at_risk": s.amount_at_risk, "currency": s.currency}
            for s in sorted(specs, key=lambda s: order.get(s.severity, 2))]


# --------------------------------------------------------------------------- clean-up


def _remove_empty_dirs(slug: str) -> None:
    if not isinstance(default_storage, FileSystemStorage):
        return
    root = os.path.realpath(default_storage.location)
    top = os.path.realpath(os.path.join(root, slug))
    if not top.startswith(root + os.sep) or not os.path.isdir(top):
        return
    for dirpath, _dirs, _files in sorted(os.walk(top), key=lambda w: -len(w[0])):
        try:
            os.rmdir(dirpath)  # only succeeds when empty
        except OSError:
            pass


def delete_submission(sub: TrySubmission) -> None:
    """Delete the uploaded file, everything read from it and the sandbox organization."""
    org = sub.organization
    for doc in Document.objects.filter(organization=org):
        try:
            if doc.file:
                doc.file.delete(save=False)
        except Exception:  # noqa: BLE001 - a missing file must not stop the clean-up
            log.warning("Could not delete file of try page document %s", doc.pk)
    slug = org.slug
    org.delete()  # cascades to the submission, documents, fields, shipments and audit rows
    _remove_empty_dirs(slug)


def purge(now: datetime | None = None) -> int:
    """Delete every expired upload. Returns how many were deleted."""
    now = now or timezone.now()
    count = 0
    for sub in TrySubmission.objects.filter(expires_at__lte=now).select_related("organization"):
        delete_submission(sub)
        count += 1
    if count:
        audit(None, "try.purged", "try", count=count)
    return count
