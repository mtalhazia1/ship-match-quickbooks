"""Optional AI check: does each line's tariff number fit its description?

Runs once after a customs entry is read (the document.extracted audit row), only when AI reading is on and
CUSTOMS_AI_HTS_CHECK=1. Each (tariff number, description) pair is asked once per organization and the answer is
saved (HtsReview), so checking a shipment again never calls the AI. A doubtful answer becomes a warning; the AI never
changes a value and never raises an error. Without AI, or when the call fails, there is simply no such check.
"""
from __future__ import annotations

import logging
import re

from django.conf import settings

from apps.documents.services import llm

from ..models import HtsReview
from .entry import LineCheck, hts_digits, is_us_entry, line_checks

log = logging.getLogger(__name__)
MAX_LINES = 40

SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "plausible": {"type": "boolean"},
                    "reason": {"type": "string", "description": "One short sentence: what the tariff number covers"},
                },
                "required": ["index", "plausible", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["lines"],
    "additionalProperties": False,
}
SYSTEM = ("You check customs tariff classifications for an importer's accounts payable team. For each line, say "
          "whether the tariff number plausibly covers the goods described. Chapter 99 numbers (additional duties such "
          "as Section 301) are always plausible. Answer plausible=false only when the number clearly covers a "
          "different kind of goods. Keep each reason to one short sentence.")


def enabled() -> bool:
    return bool(getattr(settings, "CUSTOMS_AI_HTS_CHECK", True)) and llm.is_enabled()


def description_key(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()[:200]


def _pairs(data: dict) -> list[LineCheck]:
    return [c for c in line_checks(data) if hts_digits(c.hts) and c.description.strip()
            and not hts_digits(c.hts).startswith("99")][:MAX_LINES]


def review_document(doc) -> int:
    """Ask about the document's lines not asked before. Returns how many answers were saved (0 when off or failed)."""
    if not enabled():
        return 0
    org, data = doc.organization, doc.data()
    pending = []
    for c in _pairs(data):
        key = (hts_digits(c.hts), description_key(c.description))
        if key not in {(p[0], p[1]) for p in pending} and not HtsReview.objects.filter(
                organization=org, hts_code=key[0], description_key=key[1]).exists():
            pending.append((key[0], key[1], c))
    if not pending:
        return 0
    listing = "\n".join(f"{i}. tariff number {c.hts} | description: {c.description[:200]}"
                        for i, (_, _, c) in enumerate(pending))
    country = str(data.get("country_of_origin") or "")
    where = " (imports into the United States, HTSUS)" if is_us_entry(data) else ""
    user = f"Customs entry lines{where}{f', country of origin {country}' if country else ''}:\n{listing}"
    try:
        out = llm.structured_call(SYSTEM, user, SCHEMA, name="check_tariff_numbers", purpose="customs_hts")
    except llm.LLMError as e:
        log.warning("AI tariff number check failed for document %s: %s", doc.pk, e)
        return 0
    saved = 0
    for item in (out or {}).get("lines") or []:
        try:
            hts, key, _ = pending[int(item.get("index"))]
        except (TypeError, ValueError, IndexError):
            continue
        _, created = HtsReview.objects.get_or_create(
            organization=org, hts_code=hts, description_key=key,
            defaults={"plausible": bool(item.get("plausible", True)), "reason": str(item.get("reason") or "")[:300],
                      "model": llm.model_name()[:60]})
        saved += int(created)
    return saved


def doubts_for(doc, data: dict) -> list[tuple[LineCheck, str]]:
    """Lines the AI found doubtful earlier (no AI call here)."""
    pairs = _pairs(data)
    if not pairs:
        return []
    reviews = {(r.hts_code, r.description_key): r for r in HtsReview.objects.filter(
        organization=doc.organization, plausible=False, hts_code__in={hts_digits(c.hts) for c in pairs})}
    out = []
    for c in pairs:
        r = reviews.get((hts_digits(c.hts), description_key(c.description)))
        if r is not None:
            out.append((c, r.reason or "the AI check found it doubtful"))
    return out


def on_extracted(doc_id: int) -> None:
    """After a document is read: check its tariff numbers when it is a customs entry. Never raises."""
    from apps.documents.models import Document

    try:
        doc = Document.objects.filter(pk=doc_id, doc_type=Document.DocType.CUSTOMS_ENTRY).first()
        if doc is not None:
            review_document(doc)
    except Exception:
        log.exception("AI tariff number check failed for document %s", doc_id)
