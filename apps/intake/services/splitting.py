"""Several invoices in one PDF (a carrier's batch): find where each one starts and split the file.

Page boundaries come from the text of each page:
  * strong: the invoice (or credit note) number changes; "Page 1 of N" starts again;
  * medium: the previous page was "Page N of N";
  * weak: a new document title at the top of the page; the previous page ended with a grand total.
A page that carries the same invoice number as the pages before it, says "Page 2 of 3" or says
"continued" is never a boundary, so one invoice that runs over several pages always stays whole.

Strong signals split on their own. Without AI, medium-or-stronger evidence is needed. With AI reading
on, one short structured call over the uncertain pages decides weak and medium cases.

The original file stays as the parent document (status "split") for audit; each part is a new document
that is read, matched and checked on its own. A reviewer can undo a wrong split with `keep_whole`.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.utils import audit
from apps.documents.models import Document
from apps.documents.services import llm
from apps.documents.services.ingest import RejectedFile, dispatch, ingest_bytes
from apps.documents.services.normalize import norm_ref

log = logging.getLogger(__name__)

STRONG, MEDIUM, WEAK = 3, 2, 1
AI_MAX_PAGES = 60

NUMBER_RE = re.compile(r"\b(?P<label>invoice|inv\.?|credit\s*note|credit\s*memo)\s*(?:no\.?|number|nr\.?|#)\s*[:#.]?\s*"
                       r"(?P<v>[A-Z0-9][A-Z0-9/-]{2,})", re.I)
NOT_OWN = re.compile(r"(original|against|reference|ref\.?|for|applies to|relates to|credited|your|cancels|corrects)\s*$",
                     re.I)
PAGE_OF_RE = re.compile(r"\b(?:page|pg\.?|p\.)\s*(\d{1,3})\s*(?:of|/)\s*(\d{1,3})\b", re.I)
TITLE_RE = re.compile(r"\b(commercial invoice|freight invoice|tax invoice|credit note|credit memo|debit note|"
                      r"bill of lading|invoice)\b(?!\s*(?:no\b|no\.|number|nr|#|date|:|to\b|total|amount|ref))", re.I)
CONTINUED_RE = re.compile(r"\b(continued|cont'd|cont\.|carried forward|brought forward)\b", re.I)
TOTAL_RE = re.compile(r"(\b(total due|amount due|grand total|invoice total|total amount|balance due|total credit|"
                      r"total payable|amount payable)\b|^\s*total\s*(\(?[A-Z]{3}\)?)?\s*:)", re.I)
AMOUNT_RE = re.compile(r"\d[\d,]*\.\d{2}\b")


@dataclass
class PageInfo:
    number: str | None = None        # the document's own invoice or credit note number, normalized
    printed_number: str | None = None
    page_of: tuple[int, int] | None = None
    title: bool = False
    continued: bool = False
    ends_with_total: bool = False


@dataclass
class Boundary:
    page: int                  # 0-based index of the first page of a new document
    score: int
    reasons: list[str] = field(default_factory=list)


def page_info(text: str) -> PageInfo:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    info = PageInfo()
    for ln in lines:
        for m in NUMBER_RE.finditer(ln):
            if NOT_OWN.search(ln[:m.start()]) or not re.search(r"\d", m.group("v")):
                continue
            info.number, info.printed_number = norm_ref(m.group("v")), m.group("v")
            break
        if info.number:
            break
    for ln in lines:
        m = PAGE_OF_RE.search(ln)
        if m and 1 <= int(m.group(1)) <= int(m.group(2)):
            info.page_of = (int(m.group(1)), int(m.group(2)))
            break
    top = lines[:6]
    info.title = any(TITLE_RE.search(ln) for ln in top)
    info.continued = any(CONTINUED_RE.search(ln) for ln in top)
    tail = lines[-8:]
    info.ends_with_total = any(TOTAL_RE.search(ln) and AMOUNT_RE.search(ln) and "subtotal" not in ln.lower()
                               and not CONTINUED_RE.search(ln) for ln in tail)
    return info


def find_boundaries(pages: list[str]) -> list[Boundary]:
    """Score every page after the first as the possible start of a new document."""
    infos = [page_info(p) for p in pages]
    out: list[Boundary] = []
    block_number = infos[0].number if infos else None
    for i in range(1, len(infos)):
        cur, prev = infos[i], infos[i - 1]
        b = Boundary(i, 0)
        if cur.page_of and cur.page_of[0] > 1:
            b.reasons.append(f"page {i + 1} says page {cur.page_of[0]} of {cur.page_of[1]}")
        elif cur.number and block_number and cur.number == block_number:
            b.reasons.append(f"page {i + 1} has the same number {cur.printed_number}")
        elif cur.continued:
            b.reasons.append(f"page {i + 1} says it continues")
        else:
            if cur.page_of and cur.page_of[0] == 1:
                b.score += STRONG
                b.reasons.append(f"page numbering starts again on page {i + 1}")
            if cur.number and block_number and cur.number != block_number:
                b.score += STRONG
                b.reasons.append(f"the invoice number changes to {cur.printed_number}")
            elif cur.number and not block_number:
                b.score += WEAK
                b.reasons.append(f"an invoice number ({cur.printed_number}) first appears")
            if prev.page_of and prev.page_of[0] == prev.page_of[1]:
                b.score += MEDIUM
                b.reasons.append(f"page {i} was the last page of its document")
            if cur.title:
                b.score += WEAK
                b.reasons.append("a new document title at the top")
            if prev.ends_with_total:
                b.score += WEAK
                b.reasons.append(f"page {i} ends with a total")
        if b.score > 0:
            out.append(b)
        if b.score >= MEDIUM:
            block_number = cur.number  # a new document begins (its number may be unknown)
        elif cur.number and not block_number:
            block_number = cur.number
    return out


def plan(pages: list[str]) -> tuple[list[tuple[int, int]], dict]:
    """Page ranges (0-based, inclusive) of the documents in a file, and why."""
    candidates = find_boundaries(pages)
    ai_answer = None
    uncertain = [b for b in candidates if 0 < b.score < STRONG]
    if uncertain and settings.INTAKE_SPLIT_AI_CONFIRM and llm.is_enabled() and len(pages) <= AI_MAX_PAGES:
        try:
            ai_answer = confirm_with_ai(pages)
        except llm.LLMError as e:
            log.warning("AI page check failed, using the page rules only: %s", e)
    starts = []
    for b in candidates:
        if b.score >= STRONG:
            starts.append(b)
        elif ai_answer is not None:
            if ai_answer.get(b.page):
                b.reasons.append("confirmed by AI")
                starts.append(b)
        elif b.score >= MEDIUM:
            starts.append(b)
    bounds = [0] + [b.page for b in starts] + [len(pages)]
    ranges = [(bounds[i], bounds[i + 1] - 1) for i in range(len(bounds) - 1)]
    info = {"boundaries": [{"page": b.page + 1, "score": b.score, "reasons": b.reasons} for b in starts],
            "ai_checked": ai_answer is not None}
    return ranges, info


def confirm_with_ai(pages: list[str]) -> dict[int, bool]:
    """One structured call: for each page, does a new document start there? Only the start and end of each
    page are sent, so the call stays small."""
    snippets = []
    for i, p in enumerate(pages):
        text = p.strip()
        clip = text if len(text) <= 1100 else f"{text[:800]}\n[...]\n{text[-300:]}"
        snippets.append(f"<page number=\"{i + 1}\">\n{clip}\n</page>")
    schema = {
        "type": "object",
        "properties": {"pages": {"type": "array", "items": {
            "type": "object",
            "properties": {"page": {"type": "integer"}, "starts_new_document": {"type": "boolean"},
                           "document_number": {"type": ["string", "null"]}},
            "required": ["page", "starts_new_document", "document_number"],
            "additionalProperties": False,
        }}},
        "required": ["pages"],
        "additionalProperties": False,
    }
    out = llm.structured_call(
        system=("You check where separate documents begin inside one PDF of shipping and accounting paperwork "
                "(invoices, credit notes, bills of lading). A new document starts where a different invoice or "
                "credit note begins, not where one invoice simply continues onto another page."),
        user=("For every page, say whether a new, separate document starts on it, and the invoice or credit note "
              "number printed on it (null if none). Page 1 always starts a document.\n\n" + "\n".join(snippets)),
        schema=schema, name="page_boundaries", purpose="split",
    )
    return {int(p["page"]) - 1: bool(p["starts_new_document"]) for p in out.get("pages", [])
            if isinstance(p, dict) and str(p.get("page", "")).isdigit()}


def cut(pdf_bytes: bytes, ranges: list[tuple[int, int]]) -> list[bytes]:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_bytes))
    if reader.is_encrypted:
        reader.decrypt("")
    parts = []
    for first, last in ranges:
        writer = PdfWriter()
        for n in range(first, last + 1):
            writer.add_page(reader.pages[n])
        buf = io.BytesIO()
        writer.write(buf)
        parts.append(buf.getvalue())
    return parts


def part_name(filename: str, index: int, count: int, first: int, last: int) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    pages = f"page {first + 1}" if first == last else f"pages {first + 1}-{last + 1}"
    return f"{stem[:150]} (part {index} of {count}, {pages}).pdf"


def split_if_batch(doc: Document, tr, pdf_bytes: bytes, calls: list) -> bool:
    """Called by the pipeline after the text is read. True when the document was split into parts."""
    intake = doc.intake or {}
    if not settings.INTAKE_SPLIT_PDFS or doc.source_format == Document.Format.SPREADSHEET:
        return False
    if intake.get("part") or intake.get("keep_whole") or tr.page_count < 2:
        return False
    if doc.children.exists():  # split before: a retried task or a reprocess
        doc.status, doc.error = Document.Status.SPLIT, ""
        doc.save(update_fields=["status", "error", "updated_at"])
        for child in doc.children.filter(status=Document.Status.RECEIVED):
            dispatch(child)
        return True
    pages = tr.text.split("\f")
    if len(pages) != tr.page_count:
        return False  # the text doesn't line up with the pages (an OCR reading without page breaks)
    ranges, info = plan(pages)
    if len(ranges) < 2:
        return False
    try:
        parts = cut(pdf_bytes, ranges)
    except Exception as e:  # a PDF pypdf can't rewrite: keep it whole rather than fail
        log.warning("Could not split document %s: %s", doc.pk, e)
        return False

    reused_text = tr.method in ("anthropic", "textract")
    uploader = _uploader(doc)  # the parts count as uploaded by the same person (maker-checker)
    created, records = [], []
    try:
        with transaction.atomic():  # all parts or none
            for n, ((first, last), data) in enumerate(zip(ranges, parts), 1):
                name = part_name(doc.original_filename, n, len(ranges), first, last)
                child, new = ingest_bytes(doc.organization, name, data, source=doc.source, email=doc.email,
                                          actor=uploader, process="none", parent=doc)
                records.append({"pages": [first + 1, last + 1], "document": child.pk, "duplicate": not new})
                if not new:
                    continue
                child.intake = {**(child.intake or {}), "part": {"index": n, "count": len(ranges),
                                                                 "pages": [first + 1, last + 1], "of": doc.pk}}
                if reused_text:  # scanned batch: the parts reuse the OCR text instead of paying for it again
                    child.text = "\n\f\n".join(p.strip() for p in pages[first:last + 1])
                    child.page_count = last - first + 1
                    child.intake["part"].update(text_source=tr.method, page_count=last - first + 1)
                child.save()
                created.append(child)

            doc.status, doc.error = Document.Status.SPLIT, ""
            doc.intake = {**intake, "split": {"parts": records, **info, "at": timezone.now().isoformat()}}
            doc.llm_usage = llm.summarize(calls)
            doc.save()
            audit(doc.organization, "document.split", doc, filename=doc.original_filename, parts=len(ranges),
                  pages=[r["pages"] for r in records], reasons=[b["reasons"] for b in info["boundaries"]],
                  ai_checked=info["ai_checked"])
    except RejectedFile as e:
        log.warning("Document %s was not split, a part could not be stored: %s", doc.pk, e)
        return False
    for child in created:
        dispatch(child)
    return True


def _uploader(doc: Document):
    from apps.core.models import AuditEvent

    event = (AuditEvent.objects.filter(action="document.received", object_type="Document", object_id=str(doc.pk),
                                       actor__isnull=False).select_related("actor").first())
    return event.actor if event else None


class SplitLocked(Exception):
    pass


def keep_whole(doc: Document, user) -> Document:
    """Undo a split: remove the parts and read the original file again as one document."""
    from apps.accounting.models import PostedBill
    from apps.documents.services.pipeline import process_document
    from apps.shipments.services.matching import refresh_keys
    from apps.shipments.services.validation import validate_shipment

    if doc.status != Document.Status.SPLIT:
        raise SplitLocked("This document was not split.")
    children = list(doc.children.select_related("match__shipment"))
    blockers = []
    for c in children:
        link = getattr(c, "match", None)
        if link and link.shipment.is_locked:
            blockers.append(f"{c.original_filename} is in {link.shipment.reference}, which is "
                            f"{link.shipment.get_status_display().lower()}. Reopen it first.")
        elif PostedBill.objects.filter(document=c).exists():
            blockers.append(f"{c.original_filename} was already sent to QuickBooks.")
    if blockers:
        raise SplitLocked(" ".join(blockers))
    files = [n for c in children for n in (c.file.name, c.original_file.name) if n]
    with transaction.atomic():
        shipments = {c.match.shipment_id: c.match.shipment for c in children if getattr(c, "match", None)}
        for c in children:
            c.delete()
        for s in shipments.values():
            if not s.links.exists():
                s.delete()
            else:
                refresh_keys(s)
                validate_shipment(s)
        split = dict((doc.intake or {}).get("split") or {})
        split.update(undone_at=timezone.now().isoformat(), undone_by=user.get_username())
        doc.intake = {**(doc.intake or {}), "split": split, "keep_whole": True}
        doc.status = Document.Status.RECEIVED
        doc.save(update_fields=["intake", "status", "updated_at"])
        audit(doc.organization, "document.unsplit", doc, actor=user, filename=doc.original_filename,
              parts=len(children))
        transaction.on_commit(lambda: _delete_files(files))
    return process_document(doc.pk)


def _delete_files(names: list[str]) -> None:
    from django.core.files.storage import default_storage

    for name in names:
        try:
            default_storage.delete(name)
        except Exception:
            log.warning("Could not remove %s", name)
