"""Placing documents by customs entry number, registered with matching.register_match_fallback.

Used only when a document shares no B/L, container or PO number with an open shipment:
  * a customs entry joins the shipment that already has the same entry (a corrected copy) or a document that
    prints its entry number (the broker's invoice for the duty);
  * any other document that prints a US entry number (a broker invoice without the B/L) joins the shipment
    holding that customs entry.
"""
from __future__ import annotations

from apps.documents.models import Document, ExtractedField
from apps.shipments.models import MatchLink

from .entry import normalize_entry_number, us_entry_numbers

CUSTOMS = Document.DocType.CUSTOMS_ENTRY


def _active():
    from apps.shipments.services.matching import ACTIVE

    return ACTIVE


def _candidate(shipment, reason: str):
    from apps.shipments.services.matching import SCORES, Candidate

    return Candidate(shipment, MatchLink.Method.ENTRY_NUMBER, SCORES[MatchLink.Method.ENTRY_NUMBER], reason)


def _entries_with_number(org, number: str, exclude_id: int):
    """Customs entries in open shipments with this (normalized) entry number."""
    digits = "".join(ch for ch in number if ch.isdigit())[-8:]
    qs = (ExtractedField.objects.filter(document__organization=org, document__doc_type=CUSTOMS, name="entry_number",
                                        document__match__shipment__status__in=_active())
          .exclude(document_id=exclude_id).select_related("document__match__shipment")
          .order_by("document__received_at", "document_id"))
    if digits:
        qs = qs.filter(value__icontains=digits[:4])
    for f in qs:
        if normalize_entry_number(f.value) == number:
            yield f.document


def entry_number_candidate(doc: Document):
    """A Candidate for the shipment that shares this document's customs entry number, or None."""
    org = doc.organization
    if doc.doc_type == CUSTOMS:
        number = normalize_entry_number(doc.field("entry_number"))
        if len(number) < 6:
            return None
        for other in _entries_with_number(org, number, doc.pk):
            return _candidate(other.match.shipment, f"same customs entry {doc.field('entry_number')} as "
                                                    f"{other.original_filename}")
        printed = (Document.objects.filter(organization=org, match__shipment__status__in=_active())
                   .exclude(pk=doc.pk).exclude(doc_type=CUSTOMS).select_related("match__shipment")
                   .order_by("received_at", "pk"))
        core = number[3:10] if len(number) == 11 else number[-7:]
        for other in printed.filter(text__icontains=core):
            if number in us_entry_numbers(other.text) or number in {normalize_entry_number(w) for w in other.text.split()}:
                return _candidate(other.match.shipment, f"customs entry {doc.field('entry_number')} is printed on "
                                                        f"{other.original_filename}")
        return None
    if not doc.posts_to_accounting:
        return None
    for number in us_entry_numbers(doc.text):
        for entry in _entries_with_number(org, number, doc.pk):
            return _candidate(entry.match.shipment, f"prints customs entry {entry.field('entry_number')} "
                                                    f"({entry.original_filename})")
    return None
