"""Group documents into shipments.

Order of trust:
  1. Exact B/L number             -> score 1.00
  2. Exact container number       -> score 0.95
  3. Exact PO number              -> score 0.85 (a PO can be split across shipments, so weaker)
  4. Near-match reference (fuzzy) -> score 0.70, always sent to review
  5. Credit notes only: the shipment of the invoice it credits (original invoice number) -> score 0.90
  6. Fallbacks other apps register (register_match_fallback), e.g. a customs entry number -> their own score
No match but the document has a B/L, container or PO -> it starts a new shipment.
No usable reference at all -> left unmatched for a reviewer.

Matching for one organization is serialized with a row lock, so two workers processing
documents of the same shipment at the same time cannot create two shipments.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from rapidfuzz import fuzz

from apps.core.models import Organization
from apps.core.utils import audit
from apps.documents.models import Document
from apps.documents.services.normalize import norm_ref
from apps.shipments.models import MatchLink, Shipment

from .containers import is_valid_container

log = logging.getLogger(__name__)

FUZZY_THRESHOLD = 90  # rapidfuzz ratio, 0-100
SCORES = {
    MatchLink.Method.EXACT_BL: 1.0,
    MatchLink.Method.EXACT_CONTAINER: 0.95,
    MatchLink.Method.EXACT_PO: 0.85,
    MatchLink.Method.FUZZY: 0.70,
    MatchLink.Method.ORIGINAL_INVOICE: 0.90,
    MatchLink.Method.ENTRY_NUMBER: 0.90,
}
ACTIVE = [Shipment.Status.OPEN, Shipment.Status.NEEDS_REVIEW, Shipment.Status.READY]


@dataclass
class Keys:
    bl: str
    containers: list[str]
    pos: list[str]

    @property
    def empty(self) -> bool:
        return not (self.bl or self.containers or self.pos)


@dataclass
class Candidate:
    shipment: Shipment
    method: str
    score: float
    reason: str


def doc_keys(doc: Document) -> Keys:
    data = doc.data()
    return Keys(
        bl=norm_ref(data.get("bl_number")),
        containers=[norm_ref(c) for c in data.get("container_numbers") or [] if c],
        pos=[norm_ref(p) for p in data.get("po_numbers") or [] if p],
    )


def find_candidates(org: Organization, keys: Keys, exclude_id: int | None = None) -> list[Candidate]:
    cands: list[Candidate] = []
    qs = Shipment.objects.filter(organization=org, status__in=ACTIVE)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    for s in qs:
        s_bl = norm_ref(s.bl_number)
        s_conts = {norm_ref(c) for c in s.container_numbers}
        s_pos = {norm_ref(p) for p in s.po_numbers}
        if keys.bl and s_bl and keys.bl == s_bl:
            cands.append(Candidate(s, MatchLink.Method.EXACT_BL, SCORES["exact_bl"], f"B/L {keys.bl}"))
            continue
        shared = s_conts.intersection(keys.containers)
        if shared:
            cands.append(Candidate(s, MatchLink.Method.EXACT_CONTAINER, SCORES["exact_container"],
                                   f"container {', '.join(sorted(shared))}"))
            continue
        shared_po = s_pos.intersection(keys.pos)
        if shared_po:
            cands.append(Candidate(s, MatchLink.Method.EXACT_PO, SCORES["exact_po"], f"PO {', '.join(sorted(shared_po))}"))
            continue
        fuzzy = _fuzzy_reason(keys, s_bl, s_conts)
        if fuzzy:
            cands.append(Candidate(s, MatchLink.Method.FUZZY, SCORES["fuzzy"], fuzzy))
    return sorted(cands, key=lambda c: c.score, reverse=True)


def _fuzzy_reason(keys: Keys, s_bl: str, s_conts: set[str]) -> str | None:
    if keys.bl and s_bl and fuzz.ratio(keys.bl, s_bl) >= FUZZY_THRESHOLD:
        return f"B/L {keys.bl} is close to {s_bl}"
    for c in keys.containers:
        # Only near-match containers that fail their own check digit: a valid number is a different box.
        if is_valid_container(c):
            continue
        for sc in s_conts:
            if c[:4] == sc[:4] and fuzz.ratio(c, sc) >= FUZZY_THRESHOLD:
                return f"container {c} is close to {sc} (check digit fails)"
    return None


# Other apps add ways to place a document that shares no B/L, container or PO with an open shipment:
# register_match_fallback(fn) from AppConfig.ready(); fn(doc) returns a Candidate or None.
MATCH_FALLBACKS = []


def register_match_fallback(fn) -> None:
    if fn not in MATCH_FALLBACKS:
        MATCH_FALLBACKS.append(fn)


def lock_org(org_id: int) -> None:
    """Serialize matching/validation per organization. FOR NO KEY UPDATE does not conflict with the
    FOR KEY SHARE locks Postgres takes when other rows reference the organization, avoiding deadlocks."""
    Organization.objects.select_for_update(no_key=True).get(pk=org_id)


def match_document(doc: Document) -> Shipment | None:
    """Link a document to a shipment (existing or new). Returns the shipment, or None if unmatched."""
    if hasattr(doc, "match") and doc.match.method == MatchLink.Method.MANUAL:
        return doc.match.shipment  # reviewer decisions are never overridden
    keys = doc_keys(doc)
    with transaction.atomic():
        lock_org(doc.organization_id)
        MatchLink.objects.filter(document=doc).delete()
        cands = find_candidates(doc.organization, keys) if not keys.empty else []
        if not cands and doc.is_credit:  # a credit note joins the shipment of the invoice it credits
            from apps.intake.services.credit import original_invoice_shipment

            found = original_invoice_shipment(doc)
            if found:
                cands = [Candidate(found[0], MatchLink.Method.ORIGINAL_INVOICE, SCORES["original_invoice"], found[1])]
        for fallback in MATCH_FALLBACKS if not cands else []:
            found = fallback(doc)
            if found:
                cands = [found]
                break
        if not cands and keys.empty:
            doc.status = Document.Status.UNMATCHED
            doc.save(update_fields=["status"])
            return None
        if cands:
            best = cands[0]
            shipment = best.shipment
            link = MatchLink.objects.create(document=doc, shipment=shipment, method=best.method, score=best.score,
                                            reason=best.reason)
            # A PO-only shipment that this document ties to the same B/L or container is the same shipment.
            if best.method in (MatchLink.Method.EXACT_BL, MatchLink.Method.EXACT_CONTAINER):
                for other in cands[1:]:
                    if other.method == MatchLink.Method.EXACT_PO and not other.shipment.bl_number:
                        merge_shipments(source=other.shipment, target=shipment)
        else:
            shipment = Shipment.objects.create(organization=doc.organization)
            link = MatchLink.objects.create(document=doc, shipment=shipment, method=MatchLink.Method.NEW, score=1.0,
                                            reason="no existing shipment shares a reference")
        refresh_keys(shipment)
        doc.status = Document.Status.MATCHED
        doc.save(update_fields=["status"])
    audit(doc.organization, "document.matched", doc, shipment=shipment.reference, method=link.method,
          score=link.score, reason=link.reason)
    return shipment


def refresh_keys(shipment: Shipment) -> None:
    """Recompute a shipment's references from its documents. B/L documents are authoritative for containers."""
    docs = list(shipment.documents.prefetch_related("fields"))
    bl_docs = [d for d in docs if d.doc_type == Document.DocType.BILL_OF_LADING]
    source_for_containers = bl_docs or docs
    bl = next((d.field("bl_number") for d in bl_docs if d.field("bl_number")), None) or next(
        (d.field("bl_number") for d in docs if d.field("bl_number")), ""
    )
    containers, pos = [], []
    for d in source_for_containers:
        for c in d.field("container_numbers") or []:
            c = norm_ref(c)
            if c not in containers and (is_valid_container(c) or bl_docs):
                containers.append(c)
    for d in docs:
        for p in d.field("po_numbers") or []:
            if p not in pos:
                pos.append(p)
    shipment.bl_number = bl or ""
    shipment.container_numbers = containers
    shipment.po_numbers = pos
    shipment.save(update_fields=["bl_number", "container_numbers", "po_numbers", "updated_at"])


def merge_shipments(source: Shipment, target: Shipment) -> None:
    if source.pk == target.pk:
        return
    MatchLink.objects.filter(shipment=source).update(shipment=target)
    source.issues.all().delete()
    ref = source.reference
    source.delete()
    refresh_keys(target)
    audit(target.organization, "shipment.merged", target, merged_from=ref)


def assign_manually(doc: Document, shipment: Shipment, user) -> None:
    """Reviewer moves a document to a shipment. The old shipment is deleted if left empty."""
    old = doc.match.shipment if hasattr(doc, "match") else None
    MatchLink.objects.update_or_create(
        document=doc,
        defaults={"shipment": shipment, "method": MatchLink.Method.MANUAL, "score": 1.0,
                  "reason": f"moved by {user.get_username()}"},
    )
    doc.status = Document.Status.MATCHED
    doc.save(update_fields=["status"])
    refresh_keys(shipment)
    if old and old.pk != shipment.pk:
        if not old.links.exists():
            old.delete()
        else:
            refresh_keys(old)
    audit(doc.organization, "document.moved", doc, actor=user, to=shipment.reference,
          frm=old.reference if old else None)
