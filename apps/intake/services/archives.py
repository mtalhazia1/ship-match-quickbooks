"""ZIP archives: every supported file inside becomes a document of its own.

The archive itself is kept as a document (status "archive") for audit: its original ZIP can be
downloaded, its PDF copy lists what was inside and what happened to each file, and its children are
the documents created from it.

Safety: nothing is ever written to disk from the ZIP, and an archive is refused when it has too many
files, unpacks to too much data, packs a file suspiciously tightly (a "zip bomb"), or nests archives
deeper than INTAKE_ZIP_MAX_DEPTH. Sizes are checked again while reading, because the sizes a ZIP
declares can lie. Files with unsafe paths, links, passwords or unsupported types are skipped and
listed; __MACOSX folders and hidden or system files are ignored and listed.
"""
from __future__ import annotations

import contextvars
import io
import logging
import re
import stat
import zipfile
from dataclasses import dataclass, field

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from apps.core.utils import audit
from apps.documents.models import Document
from apps.documents.services.ingest import RejectedFile, dispatch, ingest_bytes, safe_filename, store_unique

from . import formats

log = logging.getLogger(__name__)
CHUNK = 1024 * 1024
RATIO_MIN_BYTES = 1024 * 1024  # small files may compress very well (empty CSV columns); only big ones are checked
UNSUPPORTED = {
    ".xls": "older Excel file (.xls); save it as .xlsx",
    ".doc": "Word document; save it as PDF",
    ".docx": "Word document; save it as PDF",
    ".heic": "HEIC photo; export it as JPG",
    ".heif": "HEIC photo; export it as JPG",
    ".msg": "saved email; send its attachments instead",
    ".eml": "saved email; send its attachments instead",
    ".rar": "RAR archive; use ZIP instead",
    ".7z": "7-Zip archive; use ZIP instead",
    ".gif": "GIF image; save it as PNG or JPG",
    ".bmp": "BMP image; save it as PNG or JPG",
    ".ods": "OpenDocument spreadsheet; save it as .xlsx",
    ".txt": "text file",
}


@dataclass
class Budget:
    """Limits shared by an archive and the archives inside it."""

    files: int = 0
    declared: int = 0
    read: int = 0
    created: list[int] = field(default_factory=list)
    saved: list[str] = field(default_factory=list)  # storage names, removed again if the archive is refused


_budget: contextvars.ContextVar[Budget | None] = contextvars.ContextVar("zip_budget", default=None)


@dataclass
class Member:
    name: str              # path inside the ZIP
    size: int
    status: str            # added | duplicate | skipped | ignored
    reason: str = ""
    document: int | None = None
    info: zipfile.ZipInfo | None = None

    @property
    def basename(self) -> str:
        return self.name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]

    def as_dict(self) -> dict:
        return {"name": self.name[:300], "size": self.size, "status": self.status, "reason": self.reason[:300],
                "document": self.document}


def archive_depth(parent: Document | None) -> int:
    depth, node = 0, parent
    while node is not None:
        depth += node.source_format == Document.Format.ARCHIVE
        node = node.parent
    return depth


def _limit_mb(value: int) -> str:
    return f"{value:,} MB"


def plan_members(zf: zipfile.ZipFile, filename: str, budget: Budget) -> list[Member]:
    """Decide what to do with each entry before reading any of them; refuse the archive when it breaks a limit."""
    total_limit = settings.INTAKE_ZIP_MAX_TOTAL_MB * 1024 * 1024
    infos = zf.infolist()
    if len(infos) > settings.INTAKE_ZIP_MAX_FILES * 10:
        raise RejectedFile(f"{filename}: the ZIP holds {len(infos):,} entries; the limit is "
                           f"{settings.INTAKE_ZIP_MAX_FILES:,} files. Send it in smaller archives.")
    members = []
    for info in infos:
        if info.is_dir():
            continue
        name = info.filename
        m = Member(name, info.file_size, "skipped", info=info)
        parts = [p for p in name.replace("\\", "/").split("/") if p]
        ext = formats.extension(m.basename)
        mode = info.external_attr >> 16
        if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name) or ".." in parts or "\x00" in name:
            m.reason = "unsafe file path inside the ZIP"
        elif parts and parts[0] == "__MACOSX" or any(p.startswith(".") for p in parts) or formats.is_hidden(m.basename):
            m.status, m.reason = "ignored", "system or hidden file"
        elif stat.S_ISLNK(mode):
            m.reason = "a link, not a file"
        elif info.flag_bits & 0x1:
            m.reason = "password protected; send it without a password"
        elif ext not in formats.SUPPORTED_EXTENSIONS:
            m.reason = UNSUPPORTED.get(ext, "not a PDF, image, spreadsheet or ZIP file")
        elif info.file_size > formats.max_bytes(formats.ARCHIVE if ext == ".zip" else formats.PDF):
            limit = settings.INTAKE_MAX_ARCHIVE_MB if ext == ".zip" else settings.INTAKE_MAX_FILE_MB
            m.reason = f"larger than {_limit_mb(limit)}"
        else:
            m.status = "accepted"
            if info.file_size > RATIO_MIN_BYTES and \
                    info.file_size / max(info.compress_size, 1) > settings.INTAKE_ZIP_MAX_RATIO:
                raise RejectedFile(f"{filename}: {m.basename} is packed {info.file_size // max(info.compress_size, 1):,} "
                                   "times smaller than its real size, which is how harmful ZIP files ('zip bombs') "
                                   "look. The archive was not opened.")
            budget.files += 1
            budget.declared += info.file_size
            if budget.files > settings.INTAKE_ZIP_MAX_FILES:
                raise RejectedFile(f"{filename}: more than {settings.INTAKE_ZIP_MAX_FILES:,} files inside. "
                                   "Send it in smaller archives.")
            if budget.declared > total_limit:
                raise RejectedFile(f"{filename}: the files inside add up to more than "
                                   f"{_limit_mb(settings.INTAKE_ZIP_MAX_TOTAL_MB)}. Send it in smaller archives.")
        members.append(m)
    return members


def read_member(zf: zipfile.ZipFile, m: Member, filename: str, budget: Budget) -> bytes:
    """Read one file, never more than it declared and never past the archive's total limit."""
    total_limit = settings.INTAKE_ZIP_MAX_TOTAL_MB * 1024 * 1024
    chunks, n = [], 0
    with zf.open(m.info) as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            n += len(chunk)
            budget.read += len(chunk)
            if n > m.info.file_size:
                raise RejectedFile(f"{filename}: {m.basename} is bigger than the ZIP says it is. "
                                   "The archive may be damaged or harmful and was not opened.")
            if budget.read > total_limit:
                raise RejectedFile(f"{filename}: the files inside add up to more than "
                                   f"{_limit_mb(settings.INTAKE_ZIP_MAX_TOTAL_MB)}. Send it in smaller archives.")
            chunks.append(chunk)
    return b"".join(chunks)


def _reason(message: str, name: str) -> str:
    prefix = f"{name}: "
    text = message[len(prefix):] if message.startswith(prefix) else message
    return text[:1].lower() + text[1:] if text else text


def ingest_archive(org, filename: str, content: bytes, sha: str, *, source: str, email=None, actor=None,
                   process: str = "async", parent: Document | None = None) -> Document:
    budget = _budget.get()
    top = budget is None
    if top:
        budget = Budget()
        token = _budget.set(budget)
    depth = archive_depth(parent)
    mark_saved, mark_created = len(budget.saved), len(budget.created)
    try:
        if depth > settings.INTAKE_ZIP_MAX_DEPTH:   # whatever the member was called or how it started
            raise RejectedFile(f"{filename}: a ZIP inside a ZIP inside a ZIP; unpack it first")
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
        except (zipfile.BadZipFile, OSError, EOFError, ValueError, NotImplementedError) as e:
            raise RejectedFile(f"{filename}: the ZIP file is damaged and can't be opened. Create it again and send it.") from e
        with zf, transaction.atomic():
            members = plan_members(zf, filename, budget)
            if top and parent is None:   # the plan's limit counts every file inside, not the ZIP as one
                from apps.billing.usage import check_room

                check_room(org, sum(m.status == "accepted" for m in members))
            if not any(m.status == "accepted" for m in members):
                seen = {m.basename: m.reason for m in members if m.status == "skipped"}
                found = ", ".join(f"{name}: {reason}" if reason else name for name, reason in sorted(seen.items())[:5])
                raise RejectedFile(f"{filename}: no PDF, image or spreadsheet files could be added"
                                   + (f" ({found})." if found else ".") + f" Send {formats.SUPPORTED_TEXT} files.")
            archive = Document(organization=org, source=source, email=email, original_filename=filename, sha256=sha,
                               source_format=Document.Format.ARCHIVE, status=Document.Status.ARCHIVE, parent=parent)
            archive.original_file.save(safe_filename(filename), ContentFile(content), save=False)
            budget.saved.append(archive.original_file.name)
            store_unique(archive)
            for m in members:
                if m.status != "accepted":
                    continue
                try:
                    data = read_member(zf, m, filename, budget)
                except NotImplementedError:
                    m.status, m.reason = "skipped", "packed in a way that can't be opened; make the ZIP with standard compression"
                    continue
                except (zipfile.BadZipFile, OSError, EOFError, RuntimeError) as e:
                    m.status, m.reason = "skipped", f"damaged inside the ZIP ({e.__class__.__name__})"
                    continue
                try:
                    if formats.extension(m.basename) == ".zip" or data[:4] == b"PK\x03\x04":
                        kind = formats.detect(m.basename, data)
                        if kind.kind == formats.ARCHIVE and depth + 1 > settings.INTAKE_ZIP_MAX_DEPTH:
                            m.status, m.reason = "skipped", "a ZIP inside a ZIP inside a ZIP; unpack it first"
                            continue
                    doc, created = ingest_bytes(org, m.basename, data, source=source, email=email, actor=actor,
                                                process="none", parent=archive)
                except RejectedFile as e:
                    m.status, m.reason = "skipped", _reason(str(e), m.basename)
                    continue
                m.document = doc.pk
                if created:
                    m.status = "added"
                    budget.saved += [n for n in (doc.file.name, doc.original_file.name) if n]
                    if doc.status == Document.Status.RECEIVED:
                        budget.created.append(doc.pk)
                else:
                    m.status, m.reason = "duplicate", "already received"
            counts = {k: sum(m.status == k for m in members) for k in ("added", "duplicate", "skipped", "ignored")}
            if not counts["added"] and not counts["duplicate"]:
                reasons = "; ".join(f"{m.basename}: {m.reason}" for m in members if m.status == "skipped")[:400]
                raise RejectedFile(f"{filename}: none of the files inside could be added ({reasons}).")
            archive.intake = {"format": "ZIP archive", "members": [m.as_dict() for m in members], **counts}
            archive.file.save(safe_filename(f"{filename.rsplit('.', 1)[0]}_contents.pdf"),
                              ContentFile(render_manifest(filename, members)), save=False)
            budget.saved.append(archive.file.name)
            archive.save()
            audit(org, "document.received", archive, actor=actor, filename=filename, source=source,
                  bytes=len(content), format="ZIP archive", **({"parent": parent.pk} if parent else {}))
            audit(org, "document.archive_unpacked", archive, actor=actor, filename=filename, **counts)
    except RejectedFile:
        # Everything this archive created was rolled back; remove its stored files too.
        _remove_files(budget.saved[mark_saved:])
        del budget.saved[mark_saved:]
        del budget.created[mark_created:]
        raise
    finally:
        if top:
            _budget.reset(token)
    if top:
        for pk in budget.created:
            dispatch(Document.objects.get(pk=pk), process)
        archive.refresh_from_db()
    return archive


def _remove_files(names: list[str]) -> None:
    from django.core.files.storage import default_storage

    for name in names:
        try:
            default_storage.delete(name)
        except Exception:  # best effort: an orphaned file is harmless
            log.warning("Could not remove %s after refusing an archive", name)


def summary(archive: Document) -> dict:
    """For messages and the API: what was added from an archive and what was not."""
    info = archive.intake or {}
    members = info.get("members") or []
    return {
        "added": [m for m in members if m["status"] == "added"],
        "duplicate": [m for m in members if m["status"] == "duplicate"],
        "skipped": [m for m in members if m["status"] == "skipped"],
        "ignored": [m for m in members if m["status"] == "ignored"],
    }


def render_manifest(filename: str, members: list[Member]) -> bytes:
    """The archive's PDF copy: a list of the files inside and what happened to each."""
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    title = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=13, leading=16)
    body = ParagraphStyle("b", fontName="Helvetica", fontSize=8.5, leading=11)
    note = ParagraphStyle("n", fontName="Helvetica", fontSize=9, leading=12, textColor=colors.HexColor("#555555"))
    result = {"added": "Added as a document", "duplicate": "Already received", "skipped": "Not added",
              "ignored": "Ignored"}
    rows = [[Paragraph("<b>File in the archive</b>", body), Paragraph("<b>Size</b>", body),
             Paragraph("<b>Result</b>", body)]]
    for m in members:
        text = result.get(m.status, m.status) + (f": {m.reason}" if m.reason and m.status != "duplicate" else "")
        rows.append([Paragraph(escape(m.name), body), Paragraph(formats.human_size(m.size), body),
                     Paragraph(escape(text), body)])
    table = LongTable(rows, colWidths=[95 * mm, 22 * mm, 63 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c3cdd8")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    added = sum(m.status == "added" for m in members)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=f"Contents of {filename}", author="ShipMatch",
                            leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm)
    doc.build([Paragraph(escape(f"Contents of {filename}"), title), Spacer(1, 2 * mm),
               Paragraph(f"{added} of {len(members)} files were added as documents. Each one is read and matched "
                         "on its own; open them from the archive's page in ShipMatch.", note),
               Spacer(1, 5 * mm), table])
    return buf.getvalue()
