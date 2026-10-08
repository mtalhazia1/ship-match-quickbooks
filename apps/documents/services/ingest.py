"""Store incoming files exactly once and queue them for processing.

Every way a file arrives (upload form, API, email, folder import) goes through `ingest_bytes`. It accepts
PDFs, images (converted to a PDF copy), spreadsheets (converted to a readable PDF copy) and ZIP archives
(unpacked into one document per file); see apps/intake for the conversions and their limits.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import logging
import re

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction

from apps.core.models import Organization
from apps.core.utils import audit
from apps.documents.models import Document, IngestedEmail

log = logging.getLogger(__name__)
MAX_BYTES = 25 * 1024 * 1024  # default; the real limits are INTAKE_MAX_FILE_MB / INTAKE_MAX_ARCHIVE_MB


class RejectedFile(ValueError):
    pass


class DuplicateFile(Exception):
    """The same bytes were stored by someone else a moment ago (two uploads of one file at the same time)."""

    def __init__(self, existing: Document):
        super().__init__(f"duplicate of document {existing.pk}")
        self.existing = existing


def store_unique(doc: Document) -> None:
    """Save a new document, or raise DuplicateFile when another request stored the same file first.

    The check "have we seen these bytes?" and the insert are separate steps, so two requests can both pass
    the check; the database's unique (organization, sha256) then refuses the second. That refusal is the normal
    outcome of a double-click, not an error: remove the copy just written to storage and use the first."""
    try:
        with transaction.atomic():
            doc.save()
    except IntegrityError:
        existing = Document.objects.filter(organization_id=doc.organization_id, sha256=doc.sha256).first()
        if existing is None:
            raise   # some other constraint: a real problem
        for stored in (doc.file, doc.original_file):
            if stored:
                stored.delete(save=False)
        raise DuplicateFile(existing) from None


def safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "document.pdf").strip("._") or "document.pdf"
    return name[-120:]


def display_name(name: str) -> str:
    """The file's own name without any folder path or control characters, as people see it."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[\x00-\x1f\x7f]+", "", base).strip()
    return base[:255] or "document"


def ingest_bytes(org: Organization, filename: str, content: bytes, source: str = Document.Source.UPLOAD,
                 email: IngestedEmail | None = None, actor=None, process: str = "async",
                 parent: Document | None = None) -> tuple[Document, bool]:
    """Save a file and queue it. Returns (document, created). Same bytes twice -> the existing document.

    process: "async" queues a Celery task after commit, "sync" processes now, "none" only stores.
    A ZIP returns the archive's own document; the files inside become its `children`.
    """
    from apps.intake.services import formats

    filename = display_name(filename)
    kind = formats.detect(filename, content)
    formats.check_size(filename, content, kind)
    if kind.kind == formats.PDF:   # after the size check: a huge junk file is refused without being parsed
        formats.check_pdf(filename, content)
    sha = hashlib.sha256(content).hexdigest()
    existing = Document.objects.filter(organization=org, sha256=sha).first()
    if existing:
        audit(org, "document.duplicate_file", existing, actor=actor, filename=filename)
        return existing, False
    try:
        return _store(org, filename, content, kind, sha, source, email, actor, process, parent)
    except DuplicateFile as dup:   # lost a race with an identical upload
        audit(org, "document.duplicate_file", dup.existing, actor=actor, filename=filename, race=True)
        return dup.existing, False


def _store(org, filename, content, kind, sha, source, email, actor, process, parent):
    from apps.intake.services import formats

    if parent is None:  # plan limits (apps.billing); files inside a ZIP or a split PDF are part of their parent
        from apps.billing.usage import check_intake

        check_intake(org)
    if kind.kind == formats.ARCHIVE:
        from apps.intake.services.archives import ingest_archive

        return ingest_archive(org, filename, content, sha, source=source, email=email, actor=actor,
                              process=process, parent=parent), True

    pdf, info = formats.convert(filename, content, kind)
    doc = Document(organization=org, source=source, email=email, original_filename=filename, sha256=sha,
                   source_format=kind.kind, parent=parent, intake=info)
    doc.file.save(safe_filename(doc.pdf_filename), ContentFile(pdf), save=False)
    if kind.kind != formats.PDF:
        doc.original_file.save(safe_filename(filename), ContentFile(content), save=False)
    store_unique(doc)
    extra = {"format": kind.label} if kind.kind != formats.PDF else {}
    if parent is not None:
        extra["parent"] = parent.pk
    audit(org, "document.received", doc, actor=actor, filename=filename, source=source, bytes=len(content), **extra)
    return dispatch(doc, process), True


# --------------------------------------------------------------------------- processing


_mode: contextvars.ContextVar[str] = contextvars.ContextVar("ingest_mode", default="async")


@contextlib.contextmanager
def processing_mode(mode: str):
    """Documents created while another one is processed (the parts of a split PDF) follow its mode."""
    token = _mode.set(mode)
    try:
        yield
    finally:
        _mode.reset(token)


def dispatch(doc: Document, process: str | None = None) -> Document:
    """Process a stored document now ("sync"), after commit ("async"), or not at all ("none").
    Without `process`, follow the document being processed right now (async outside a pipeline run)."""
    process = process or _mode.get()
    if process == "sync":
        from .pipeline import process_document

        with processing_mode("sync"):
            return process_document(doc.pk)
    if process == "async":
        from apps.documents.tasks import process_document_task

        pk = doc.pk
        transaction.on_commit(lambda: process_document_task.delay(pk))
    return doc
