"""Freight invoices that cover several shipments.

A forwarder often sends one invoice for several B/Ls or containers that ShipMatch keeps as separate
shipments. The invoice stays matched to one shipment (its MatchLink, the "primary" shipment), and each
shipment it covers carries a share (InvoiceAllocation). The shares always add up to the invoice total.

Detection (`sync`) runs when an invoice is matched, moved or corrected, and when another shipment
appears that the invoice names. An invoice is shared when it names another shipment by B/L number,
container or PO (in its fields or anywhere in its text). References the primary shipment also has are
ignored: a PO split over two shipments, or a container reused months later, is not a reason to share.

How the total is split, in order of preference:
  * lines:      lines that name one shipment (its B/L, a container or a PO) go to that shipment whole;
                the other lines are split by containers (or equally);
  * containers: by number of containers per shipment;
  * weight / volume: by the weight or volume on each shipment's commercial invoice lines;
  * equal;
  * manual:     amounts typed by a reviewer, which must add up to the invoice total exactly.
A basis that can't be used (a shipment without weights) falls back to containers, then equal, and
says so. Every line is split in whole cents with the largest-remainder method, so each shipment's
lines add up to its share and each line's parts add up to the line.

Nothing from outside acts on its own: a detected split is a suggestion until a person confirms it (or
saves their own split). Until then neither the primary shipment nor any shipment with a share can be
approved, and the primary shipment can't be approved before every other shipment with a share is,
because the invoice is posted once, with the primary shipment.

Integration points:
  * `bill_lines(doc)`: bill lines per shipment for the accounting system, or None for an invoice that isn't shared.
  * `posting_blockers(shipment)`: reasons the shipment's bills must not be posted yet.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.utils import audit
from apps.documents.models import Document
from apps.documents.services.normalize import norm_ref, norm_text
from apps.shipments.models import Shipment, ValidationIssue
from apps.shipments.services.containers import find_containers

from ..models import InvoiceAllocation, SharedInvoice
from .charges import document_lines, invoice_number
from .rounding import CENT, from_cents, split, split_table, to_cents

log = logging.getLogger(__name__)
Basis = SharedInvoice.Basis
DONE = {Shipment.Status.APPROVED, Shipment.Status.POSTED}
AUTO_BASES = [Basis.LINES, Basis.CONTAINERS, Basis.WEIGHT, Basis.VOLUME, Basis.EQUAL]


class SplitError(ValueError):
    """A split a reviewer asked for can't be saved; the message says why and what to do."""


@dataclass
class Party:
    shipment: Shipment
    refs: list[str] = field(default_factory=list)
    primary: bool = False

    @property
    def tokens(self) -> set[str]:
        """Normalized references that name this shipment in a line's text."""
        out = set()
        for r in self.refs:
            value = norm_ref(r.split(" ", 1)[-1])
            if len(value) >= 4:
                out.add(value)
        return out


@dataclass
class Split:
    parties: list[Party]
    lines: list[tuple[str, int]]
    parts: list[list[int]]          # parts[party][line], in cents
    basis: str
    notes: list[str] = field(default_factory=list)
    owners: list[int | None] = field(default_factory=list)

    @property
    def shares(self) -> list[int]:
        return [sum(row) for row in self.parts]


def enabled() -> bool:
    return bool(getattr(settings, "SHARED_INVOICE_DETECT", True))


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(CENT) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def eligible(doc: Document) -> bool:
    return doc.doc_type == Document.DocType.FREIGHT_INVOICE and hasattr(doc, "match")


def invoice_total(doc: Document, data: dict | None = None) -> tuple[Decimal | None, str]:
    data = data if data is not None else doc.data()
    return _dec(data.get("total_amount")), (data.get("currency") or doc.organization.home_currency).upper()[:3]


# --------------------------------------------------------------------------- who the invoice names


def _own_refs(shipment: Shipment) -> tuple[str, set[str], set[str]]:
    return (norm_ref(shipment.bl_number), {norm_ref(c) for c in shipment.container_numbers or []},
            {norm_ref(p) for p in shipment.po_numbers or []})


def find_parties(doc: Document, data: dict | None = None) -> tuple[list[Party], list[Party]]:
    """(primary first, then open shipments the invoice names; approved shipments it names)."""
    data = data if data is not None else doc.data()
    primary = doc.match.shipment
    descriptions = " ".join(str((i or {}).get("description") or "") for i in data.get("line_items") or []
                            if isinstance(i, dict))
    text = norm_text(f"{doc.text}\n{descriptions}")
    inv_bl = norm_ref(data.get("bl_number"))
    inv_containers = {norm_ref(c) for c in data.get("container_numbers") or []} | set(find_containers(
        f"{doc.text}\n{descriptions}"))
    inv_pos = {norm_ref(p) for p in data.get("po_numbers") or []}
    p_bl, p_containers, p_pos = _own_refs(primary)

    def named(s: Shipment, exclude_own: bool) -> list[str]:
        bl, containers, pos = _own_refs(s)
        refs = []
        if len(bl) >= 6 and (not exclude_own or bl != p_bl) and (bl == inv_bl or bl in text):
            refs.append(f"B/L {s.bl_number}")
        for c in sorted(containers):
            if c in inv_containers and (not exclude_own or c not in p_containers):
                refs.append(f"container {c}")
        for p in s.po_numbers or []:
            n = norm_ref(p)
            if len(n) >= 4 and n in inv_pos and (not exclude_own or n not in p_pos):
                refs.append(f"PO {p}")
        return refs

    open_parties, locked = [], []
    candidates = (Shipment.objects.filter(organization=doc.organization).exclude(pk=primary.pk)
                  .exclude(status=Shipment.Status.POSTED).order_by("pk"))
    for s in candidates:
        refs = named(s, exclude_own=True)
        if refs:
            (locked if s.is_locked else open_parties).append(Party(s, refs))
    return [Party(primary, named(primary, exclude_own=False), primary=True), *open_parties], locked


def _tokens(parties: list[Party]) -> list[set[str]]:
    """Each party's references, without the ones two parties share (they name neither)."""
    sets = [p.tokens | {t for t in (norm_ref(p.shipment.bl_number),) if len(t) >= 6}
            | {norm_ref(c) for c in p.shipment.container_numbers or []} for p in parties]
    common = set()
    for i, a in enumerate(sets):
        for b in sets[i + 1:]:
            common |= a & b
    return [s - common for s in sets]


def line_owners(lines: list[tuple[str, int]], parties: list[Party]) -> list[int | None]:
    """For each line, the one party its text names, or None (names none, or several)."""
    toks = _tokens(parties)
    owners = []
    for desc, _ in lines:
        text = norm_text(desc)
        hits = {i for i, ts in enumerate(toks) if any(t in text for t in ts)}
        owners.append(hits.pop() if len(hits) == 1 else None)
    return owners


# --------------------------------------------------------------------------- split math


def _goods_measure(shipment: Shipment, key: str) -> Decimal | None:
    """Total weight_kg or volume_cbm on the shipment's commercial invoice lines; None if any line lacks it."""
    total, seen = Decimal("0"), False
    for d in shipment.documents.filter(doc_type=Document.DocType.COMMERCIAL_INVOICE).prefetch_related("fields"):
        for item in d.field("line_items") or []:
            if not isinstance(item, dict):
                continue
            value = _measure(item.get(key))
            if value is None:
                return None
            total += value
            seen = True
    return total if seen else None


def _measure(value) -> Decimal | None:
    try:
        v = Decimal(str(value).replace(",", "")) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None
    return v if v is not None and v >= 0 else None


def party_weights(parties: list[Party], basis: str, notes: list[str]) -> tuple[list[Decimal], str]:
    """Weights for splitting between shipments, falling back (with a note) when the basis can't be used."""
    names = {Basis.WEIGHT: ("weight", "weight_kg"), Basis.VOLUME: ("volume", "volume_cbm")}
    if basis in names:
        word, key = names[basis]
        values = [_goods_measure(p.shipment, key) for p in parties]
        if all(v is not None for v in values) and sum(values) > 0:
            return values, basis
        missing = [p.shipment.reference for p, v in zip(parties, values) if v is None]
        notes.append(f"Split by containers instead of {word}: "
                     + (f"{', '.join(missing)} has no {word} on its commercial invoice lines." if missing
                        else f"no shipment has a {word}."))
        basis = Basis.CONTAINERS
    if basis == Basis.CONTAINERS:
        counts = [Decimal(len(p.shipment.container_numbers or [])) for p in parties]
        if all(counts):
            return counts, basis
        missing = [p.shipment.reference for p, c in zip(parties, counts) if not c]
        notes.append(f"Split equally instead of by containers: {', '.join(missing)} has no container numbers.")
    return [Decimal(1)] * len(parties), Basis.EQUAL


def compute(doc: Document, parties: list[Party], basis: str, data: dict | None = None,
            targets: list[int] | None = None) -> Split:
    """Split the invoice's lines between the parties. `targets` (cents per party) for a manual split."""
    lines = document_lines(doc, data)
    cols = [c for _, c in lines]
    notes: list[str] = []
    if basis == Basis.MANUAL:
        return Split(parties, lines, split_table(cols, list(targets or [])), Basis.MANUAL, notes,
                     [None] * len(lines))
    owners = line_owners(lines, parties) if basis == Basis.LINES else [None] * len(lines)
    if basis == Basis.LINES:
        weights, used = party_weights(parties, Basis.CONTAINERS, notes)
        if any(o is None for o in owners):
            rest = sum(1 for o in owners if o is None)
            how = "by containers" if used == Basis.CONTAINERS else "equally"
            notes.append(f"{rest} line{'s' if rest != 1 else ''} that name{'s' if rest == 1 else ''} no single "
                         f"shipment {'were' if rest != 1 else 'was'} split {how}.")
    else:
        weights, used = party_weights(parties, basis, notes)
        basis = used
    # Lines that name one shipment go to it whole. The rest is split as one amount (so each share is within a
    # cent of its exact proportion), then spread back over those lines so every line still adds up.
    parts = [[0] * len(lines) for _ in parties]
    rest = [0] * len(lines)
    for c, (_, cents) in enumerate(lines):
        if owners[c] is not None:
            parts[owners[c]][c] = cents
        else:
            rest[c] = cents
    if any(rest):
        rest_shares = split(sum(rest), weights)
        try:
            table_ = split_table(rest, rest_shares)
        except ValueError:  # lines that cancel out (a charge and its refund): split each line on its own
            table_ = [[0] * len(lines) for _ in parties]
            for c, cents in enumerate(rest):
                for r, part in enumerate(split(cents, weights)):
                    table_[r][c] = part
        for r in range(len(parties)):
            for c in range(len(lines)):
                parts[r][c] += table_[r][c]
    return Split(parties, lines, parts, basis, notes, owners)


def default_basis(doc: Document, parties: list[Party], data: dict | None = None) -> str:
    owners = line_owners(document_lines(doc, data), parties)
    return Basis.LINES if any(o is not None for o in owners) else Basis.CONTAINERS


def split_hash(total: Decimal | None, currency: str, rows) -> str:
    key = "|".join(sorted(f"{r.shipment_id}:{r.amount:.2f}:{r.currency}" for r in rows))
    return hashlib.sha1(f"{key}#{total}{currency}".encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- reading the stored split


def allocations(doc: Document) -> list[InvoiceAllocation]:
    return list(InvoiceAllocation.objects.filter(document=doc).select_related("shipment").order_by("id"))


def active(doc: Document) -> SharedInvoice | None:
    """The doc's shared-invoice record when it is split over two or more shipments."""
    si = SharedInvoice.objects.filter(document=doc, status=SharedInvoice.Status.ACTIVE).first()
    if si and InvoiceAllocation.objects.filter(document=doc).count() >= 2:
        return si
    return None


def is_confirmed(si: SharedInvoice, rows=None) -> bool:
    rows = rows if rows is not None else allocations(si.document)
    total, cur = invoice_total(si.document)
    return bool(si.confirmed_hash) and si.confirmed_hash == split_hash(total, cur, rows)


def adds_up(doc: Document, rows=None) -> bool:
    rows = rows if rows is not None else allocations(doc)
    total, cur = invoice_total(doc)
    return (total is not None and sum((r.amount for r in rows), Decimal("0.00")) == total
            and all(r.currency == cur for r in rows))


def frozen_by(doc: Document, rows=None) -> list[Shipment]:
    """Approved or posted shipments in the split (or the invoice's own): the split can't change then."""
    rows = rows if rows is not None else allocations(doc)
    ships = {r.shipment.pk: r.shipment for r in rows}
    if hasattr(doc, "match"):
        ships.setdefault(doc.match.shipment.pk, doc.match.shipment)
    return [s for s in ships.values() if s.is_locked]


def table(doc: Document, rows=None, data: dict | None = None) -> Split | None:
    """The stored split as lines per shipment. Recomputes the basis when that reproduces the stored shares,
    otherwise spreads every line in proportion to the stored shares (manual splits, changed data)."""
    rows = rows if rows is not None else allocations(doc)
    if len(rows) < 2:
        return None
    data = data if data is not None else doc.data()
    si = SharedInvoice.objects.filter(document=doc).first()
    primary_id = doc.match.shipment_id if hasattr(doc, "match") else None
    parties = [Party(r.shipment, _stored_refs(si, r.shipment_id), primary=r.shipment_id == primary_id) for r in rows]
    targets = [to_cents(r.amount) for r in rows]
    lines = document_lines(doc, data)
    basis = si.basis if si else Basis.MANUAL
    if basis != Basis.MANUAL:
        try:
            computed = compute(doc, parties, basis, data)
            if computed.shares == targets:
                return computed
        except ValueError:
            pass
    if sum(c for _, c in lines) == sum(targets) and all(t > 0 for t in targets):
        return Split(parties, lines, split_table([c for _, c in lines], targets), Basis.MANUAL, [], [None] * len(lines))
    # The split no longer adds up (the invoice was corrected): one line per share, as stored.
    label = f"Share of invoice {invoice_number(doc, data)}"
    return Split(parties, [(label, t) for t in targets],
                 [[t if i == r else 0 for i in range(len(targets))] for r, t in enumerate(targets)], Basis.MANUAL,
                 [], [None] * len(targets))


def _stored_refs(si: SharedInvoice | None, shipment_id: int) -> list[str]:
    return list(((si.detected or {}).get("refs") or {}).get(str(shipment_id), [])) if si else []


def parts_for(doc: Document, shipment: Shipment) -> list[tuple[str, int]] | None:
    """This shipment's lines of a shared invoice (cents). None when the invoice isn't shared."""
    if active(doc) is None:
        return None
    split_ = table(doc)
    if split_ is None:
        return None
    for r, p in enumerate(split_.parties):
        if p.shipment.pk == shipment.pk:
            return [(desc, split_.parts[r][c]) for c, (desc, _) in enumerate(split_.lines) if split_.parts[r][c]]
    return []


def shared_with(shipment: Shipment, exclude=()) -> list[Document]:
    """Invoices matched to other shipments in which this shipment carries a share."""
    ids = (InvoiceAllocation.objects.filter(shipment=shipment).exclude(document_id__in=list(exclude))
           .values_list("document_id", flat=True))
    return list(Document.objects.filter(pk__in=list(ids), shared_invoice__status=SharedInvoice.Status.ACTIVE)
                .select_related("match__shipment", "organization").prefetch_related("fields"))


def for_shipment(shipment: Shipment) -> list[SharedInvoice]:
    """Shared-invoice records that concern this shipment: its own invoices, and invoices it has a share in."""
    return list(SharedInvoice.objects.filter(
        Q(document__match__shipment=shipment) | Q(document__allocations__shipment=shipment))
        .select_related("document__match__shipment", "document__organization", "confirmed_by", "updated_by")
        .distinct().order_by("pk"))


# --------------------------------------------------------------------------- integration points


def bill_lines(doc: Document) -> list[dict] | None:
    """Bill lines for posting a shared invoice: every line split per shipment.

    Returns [{"description", "amount" (Decimal), "shipment_reference"}], adding up to the invoice total
    exactly, or None when the invoice isn't shared (post it as usual). Zero amounts are left out.
    """
    if active(doc) is None:
        return None
    rows = allocations(doc)
    if not adds_up(doc, rows):
        return None  # posting_blockers() stops posting until the split is fixed
    split_ = table(doc, rows)
    if split_ is None:
        return None
    out = []
    for r, party in enumerate(split_.parties):
        ref = party.shipment.reference
        for c, (desc, _) in enumerate(split_.lines):
            cents = split_.parts[r][c]
            if cents:
                out.append({"description": f"{desc} ({ref})"[:4000], "amount": from_cents(cents),
                            "shipment_reference": ref})
    return out


def posting_blockers(shipment: Shipment) -> list[str]:
    """Why the shipment's bills can't be posted yet because of invoices it shares with other shipments."""
    reasons = []
    docs = Document.objects.filter(match__shipment=shipment, shared_invoice__status=SharedInvoice.Status.ACTIVE)
    for doc in docs.prefetch_related("fields"):
        rows = allocations(doc)
        if len(rows) < 2:
            continue
        number = invoice_number(doc)
        si = doc.shared_invoice
        if not adds_up(doc, rows):
            reasons.append(f"The shares of invoice {number} don't add up to its total. Fix the split first.")
        elif not is_confirmed(si, rows):
            reasons.append(f"The split of invoice {number} hasn't been confirmed.")
        waiting = [r.shipment.reference for r in rows if r.shipment_id != shipment.pk and r.shipment.status not in DONE]
        if waiting:
            reasons.append(f"Invoice {number} is shared with {_and(waiting)}, which must be approved before it is "
                           "posted.")
    return reasons


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# --------------------------------------------------------------------------- keeping the split current


def sync(doc: Document) -> set[int]:
    """Detect or refresh the split of one invoice. Returns the shipments whose share changed.

    Never changes a split once a shipment in it is approved, never overrides a reviewer's own split or
    their "not shared" decision, and never creates a share for an approved shipment (it is noted instead).
    """
    doc = (Document.objects.select_related("organization", "match__shipment").prefetch_related("fields")
           .filter(pk=doc.pk).first())
    if doc is None:
        return set()
    si = SharedInvoice.objects.filter(document=doc).first()
    rows = allocations(doc)
    if frozen_by(doc, rows):
        return set()
    if not eligible(doc):
        return _drop(doc, si, rows, delete_record=True)
    data = doc.data()
    total, currency = invoice_total(doc, data)
    if total is None or total <= 0:
        return _drop(doc, si, rows, delete_record=si is not None and si.basis != Basis.MANUAL)
    if si and si.status == SharedInvoice.Status.DISMISSED:
        return _drop(doc, si, rows, delete_record=False)
    parties, locked = find_parties(doc, data)
    detected = {"refs": {str(p.shipment.pk): p.refs for p in parties},
                "locked": [{"id": p.shipment.pk, "reference": p.shipment.reference, "refs": p.refs} for p in locked]}
    if si and si.basis == Basis.MANUAL and len(rows) >= 2 and any(r.shipment_id == doc.match.shipment_id for r in rows):
        known = {str(r.shipment_id) for r in rows}
        si.detected = {**detected, "refs": {k: v for k, v in detected["refs"].items() if k in known},
                       "unshared": [p.shipment.pk for p in parties if str(p.shipment.pk) not in known],
                       "notes": (si.detected or {}).get("notes", [])}
        # The reviewer's amounts stay. If the invoice itself changed, every shipment in the split is checked again.
        changed = {r.shipment_id for r in rows} if (si.total, si.currency) != (total, currency) else set()
        si.total, si.currency = total, currency
        si.save(update_fields=["detected", "total", "currency", "updated_at"])
        return changed
    if len(parties) < 2:
        changed = _drop(doc, si, rows, delete_record=False)
        if locked and (si or enabled()):
            si = si or SharedInvoice(organization=doc.organization, document=doc)
            si.status, si.basis, si.total, si.currency = SharedInvoice.Status.ACTIVE, Basis.CONTAINERS, total, currency
            si.detected = detected
            si.save()
        elif si:
            si.delete()
        return changed
    if si is None and not enabled():
        return set()
    basis = si.basis if si and si.basis in AUTO_BASES else default_basis(doc, parties, data)
    result = compute(doc, parties, basis, data)
    new = si is None
    si = si or SharedInvoice(organization=doc.organization, document=doc)
    si.status, si.basis, si.total, si.currency = SharedInvoice.Status.ACTIVE, basis, total, currency
    si.detected = {**detected, "notes": result.notes}
    si.save()
    changed = _write_rows(doc, si, result, currency, rows, actor=None)
    if new:
        audit(doc.organization, "shared_invoice.detected", doc, invoice=invoice_number(doc, data),
              shipments=[p.shipment.reference for p in parties], basis=si.get_basis_display().lower(),
              shares={p.shipment.reference: f"{from_cents(s):.2f}" for p, s in zip(parties, result.shares)})
    return changed


def _drop(doc, si, rows, delete_record: bool) -> set[int]:
    changed = {r.shipment_id for r in rows}
    if rows:
        InvoiceAllocation.objects.filter(document=doc).delete()
    if si and delete_record:
        si.delete()
    return changed


def _reason(party: Party, split_: Split, index: int) -> str:
    named = sum(1 for o in split_.owners if o == index)
    bits = []
    if party.refs:
        bits.append("named on the invoice: " + ", ".join(party.refs[:4]) + (" and more" if len(party.refs) > 4 else ""))
    if split_.basis == Basis.LINES and named:
        bits.append(f"{named} line{'s' if named != 1 else ''} name{'s' if named == 1 else ''} it")
    return "; ".join(bits)[:300]


def _write_rows(doc, si, split_: Split, currency: str, before: list[InvoiceAllocation], actor) -> set[int]:
    old = {r.shipment_id: r for r in before}
    keep = set()
    changed = set()
    for i, (party, cents) in enumerate(zip(split_.parties, split_.shares)):
        amount = from_cents(cents)
        sid = party.shipment.pk
        keep.add(sid)
        row = old.get(sid)
        reason = _reason(party, split_, i)
        if row is None:
            InvoiceAllocation.objects.create(organization=doc.organization, document=doc, shipment=party.shipment,
                                             amount=amount, currency=currency, basis=split_.basis, reason=reason,
                                             created_by=actor)
            changed.add(sid)
        elif (row.amount, row.currency, row.basis, row.reason) != (amount, currency, split_.basis, reason):
            if (row.amount, row.currency) != (amount, currency):
                changed.add(sid)
            row.amount, row.currency, row.basis, row.reason = amount, currency, split_.basis, reason
            row.save(update_fields=["amount", "currency", "basis", "reason", "updated_at"])
    gone = [sid for sid in old if sid not in keep]
    if gone:
        InvoiceAllocation.objects.filter(document=doc, shipment_id__in=gone).delete()
        changed |= set(gone)
    return changed


def revalidate(shipment_ids, skip=()) -> None:
    from apps.shipments.services.validation import validate_shipment

    for s in Shipment.objects.filter(pk__in=[i for i in set(shipment_ids) if i not in set(skip)]):
        if not s.is_locked:
            validate_shipment(s)


def sync_related(doc: Document, skip=(), repair_splits: bool = False) -> set[int]:
    """After a document joins or leaves a shipment: refresh that invoice, and invoices elsewhere that name the
    shipment it is now in (a B/L arriving after the forwarder's invoice). repair_splits: also re-check splits
    that lost a shipment (after a move, a shipment left empty is deleted). Returns changed shipment ids."""
    changed = set()
    if doc.doc_type == Document.DocType.FREIGHT_INVOICE:
        changed |= sync(doc)
    shipment = doc.match.shipment if hasattr(doc, "match") else None
    if shipment is not None:
        for other in invoices_naming(shipment):
            if other.pk != doc.pk:
                changed |= sync(other)
    if repair_splits:
        changed |= repair(doc.organization)
    revalidate(changed, skip=skip)
    return changed


def invoices_naming(shipment: Shipment) -> list[Document]:
    """Freight invoices in other open shipments whose text names this shipment's B/L or containers."""
    refs = [r for r in [shipment.bl_number, *(shipment.container_numbers or [])] if r and len(norm_ref(r)) >= 6]
    if not refs:
        return []
    q = Q()
    for r in refs:
        q |= Q(text__icontains=r) | Q(fields__name__in=["bl_number", "container_numbers"], fields__value__icontains=r)
    return list(Document.objects.filter(organization=shipment.organization, doc_type=Document.DocType.FREIGHT_INVOICE,
                                        match__isnull=False)
                .exclude(match__shipment=shipment)
                .exclude(match__shipment__status__in=[Shipment.Status.APPROVED, Shipment.Status.POSTED])
                .filter(q).distinct()[:50])


def repair(org) -> set[int]:
    """Re-check splits that lost a shipment (merged or deleted) or no longer add up to their invoice total."""
    from django.db.models import Count, F, Sum

    broken = (SharedInvoice.objects.filter(organization=org, status=SharedInvoice.Status.ACTIVE)
              .annotate(n=Count("document__allocations"), assigned=Sum("document__allocations__amount"))
              .filter(n__gt=0).filter(Q(n__lt=2) | ~Q(assigned=F("total"))).select_related("document"))
    changed = set()
    for si in broken:
        changed |= sync(si.document)
    return changed


def reopened(shipment: Shipment) -> set[int]:
    """A shipment was reopened: invoices that named it while it was approved can now give it a share."""
    changed = set()
    for si in SharedInvoice.objects.filter(organization=shipment.organization, status=SharedInvoice.Status.ACTIVE):
        if any(item.get("id") == shipment.pk for item in (si.detected or {}).get("locked") or []):
            changed |= sync(si.document)
    revalidate(changed, skip=[shipment.pk])
    return changed


# --------------------------------------------------------------------------- what reviewers do


def _check_open(doc: Document, rows) -> None:
    frozen = frozen_by(doc, rows)
    if frozen:
        refs = _and([s.reference for s in frozen])
        raise SplitError(f"{refs} {'is' if len(frozen) == 1 else 'are'} approved, so the split is locked. "
                         f"Reopen {'it' if len(frozen) == 1 else 'them'} to change the split.")


@transaction.atomic
def save_split(doc: Document, user, basis: str, amounts: dict[int, str] | None = None, add=(), remove=()) -> Split:
    """A reviewer chooses how the invoice is split (and so confirms it). amounts: {shipment_id: "1240.00"}."""
    if not eligible(doc):
        raise SplitError("Only freight invoices that are in a shipment can be split.")
    if basis not in Basis.values:
        raise SplitError("Choose how to split the invoice.")
    rows = allocations(doc)
    _check_open(doc, rows)
    data = doc.data()
    total, currency = invoice_total(doc, data)
    if total is None or total <= 0:
        raise SplitError("The invoice has no total above zero. Type the total from the PDF first.")
    primary = doc.match.shipment
    si = SharedInvoice.objects.filter(document=doc).first()
    detected_refs = ((si.detected or {}).get("refs") or {}) if si else {}
    members: dict[int, Shipment] = {primary.pk: primary}
    for r in rows:
        members[r.shipment_id] = r.shipment
    if not rows:  # starting from scratch: the shipments the invoice names
        for p in find_parties(doc, data)[0]:
            members.setdefault(p.shipment.pk, p.shipment)
            detected_refs[str(p.shipment.pk)] = p.refs
    for s in Shipment.objects.filter(organization=doc.organization, pk__in=[int(i) for i in add]):
        if s.is_locked:
            raise SplitError(f"{s.reference} is approved, so it can't take a share. Reopen it first.")
        members[s.pk] = s
    for sid in remove:
        if int(sid) == primary.pk:
            raise SplitError(f"{primary.reference} can't be removed: the invoice is in that shipment. Move the "
                             "invoice first, or choose 'Not shared'.")
        members.pop(int(sid), None)
    if len(members) < 2:
        raise SplitError("A shared invoice needs at least two shipments. Add another shipment, or choose "
                         "'Not shared' to keep the whole invoice on this shipment.")
    parties = [Party(s, detected_refs.get(str(s.pk), []), primary=s.pk == primary.pk)
               for s in sorted(members.values(), key=lambda s: (s.pk != primary.pk, s.pk))]
    targets = None
    if basis == Basis.MANUAL:
        targets = []
        for p in parties:
            raw = (amounts or {}).get(p.shipment.pk)
            value = _dec(str(raw).replace(",", "").strip()) if raw not in (None, "") else None
            if value is None:
                raise SplitError(f"Type the share for {p.shipment.reference}.")
            if value <= 0:
                raise SplitError(f"Each share must be above zero ({p.shipment.reference} has {value:,.2f}). Remove a "
                                 "shipment from the split instead of giving it nothing.")
            targets.append(to_cents(value))
        if sum(targets) != to_cents(total):
            diff = from_cents(to_cents(total) - sum(targets))
            raise SplitError(f"The shares add up to {currency} {from_cents(sum(targets)):,.2f}, but the invoice "
                             f"total is {currency} {total:,.2f} ({'short by' if diff > 0 else 'over by'} "
                             f"{abs(diff):,.2f}). They must add up exactly.")
    try:
        result = compute(doc, parties, basis, data, targets)
    except ValueError as e:
        raise SplitError(f"The invoice can't be split that way: {e}.") from e
    before = {r.shipment.reference: f"{r.amount:.2f}" for r in rows}
    si = si or SharedInvoice(organization=doc.organization, document=doc)
    si.status, si.basis, si.total, si.currency, si.updated_by = (SharedInvoice.Status.ACTIVE, result.basis, total,
                                                                 currency, user)
    si.detected = {**(si.detected or {}), "refs": {str(p.shipment.pk): p.refs for p in parties},
                   "notes": result.notes}
    si.save()
    changed = _write_rows(doc, si, result, currency, rows, actor=user)
    after = {p.shipment.reference: f"{from_cents(s):.2f}" for p, s in zip(parties, result.shares)}
    audit(doc.organization, "shared_invoice.split_changed", doc, actor=user, invoice=invoice_number(doc, data),
          basis=si.get_basis_display().lower(), before=before, after=after)
    _confirm(doc, si, user, note="Split saved")
    revalidate(changed | {p.shipment.pk for p in parties})
    return result


@transaction.atomic
def confirm(doc: Document, user) -> None:
    si = active(doc)
    if si is None:
        raise SplitError("This invoice isn't split between shipments.")
    rows = allocations(doc)
    _check_open(doc, rows)
    if not adds_up(doc, rows):
        raise SplitError("The shares don't add up to the invoice total. Change the split so they do, then confirm.")
    _confirm(doc, si, user, note="Split confirmed")
    revalidate([r.shipment_id for r in rows])


def _confirm(doc: Document, si: SharedInvoice, user, note: str) -> None:
    """Record who confirmed this exact split, close the split warnings, and explain the containers of the other
    shipments that the invoice lists (they are on those shipments' B/Ls, which carry their shares)."""
    from apps.documents.models import Document as Doc

    rows = allocations(doc)
    total, cur = invoice_total(doc)
    si.confirmed_hash, si.confirmed_by, si.confirmed_at = split_hash(total, cur, rows), user, timezone.now()
    si.save(update_fields=["confirmed_hash", "confirmed_by", "confirmed_at", "updated_at"])
    number = invoice_number(doc)
    now = timezone.now()
    for issue in ValidationIssue.objects.filter(shipment_id__in=[r.shipment_id for r in rows], resolved=False,
                                                code="shared_invoice_split", data__invoice=doc.pk):
        _resolve(issue, user, now, f"{note} for invoice {number}.")
    primary = doc.match.shipment
    on_other_bls = {}
    for r in rows:
        if r.shipment_id == primary.pk:
            continue
        for bl in Doc.objects.filter(match__shipment_id=r.shipment_id, doc_type=Doc.DocType.BILL_OF_LADING):
            for c in bl.field("container_numbers") or []:
                on_other_bls.setdefault(norm_ref(c), r.shipment.reference)
    for issue in ValidationIssue.objects.filter(shipment=primary, document=doc, code="container_not_on_bl",
                                                resolved=False):
        container = (issue.data or {}).get("key", "")
        if container in on_other_bls:
            _resolve(issue, user, now, f"Container {container} is on the bill of lading of {on_other_bls[container]}, "
                                       f"which carries its share of this invoice. {note}.")
    audit(doc.organization, "shared_invoice.confirmed", doc, actor=user, invoice=number,
          shares={r.shipment.reference: f"{r.amount:.2f}" for r in rows})
    for r in rows:
        if r.shipment_id != primary.pk:
            audit(doc.organization, "shared_invoice.share_confirmed", r.shipment, actor=user, invoice=number,
                  share=f"{r.amount:.2f}", currency=r.currency, primary=primary.reference)


def _resolve(issue: ValidationIssue, user, when, note: str) -> None:
    issue.resolved, issue.resolved_by, issue.resolved_at, issue.resolution_note = True, user, when, note[:500]
    issue.save(update_fields=["resolved", "resolved_by", "resolved_at", "resolution_note"])
    audit(issue.organization, "issue.resolved", issue, actor=user, code=issue.code, severity=issue.severity, note=note)


@transaction.atomic
def dismiss(doc: Document, user, note: str) -> set[int]:
    """'Not shared': keep the whole invoice on its own shipment and stop suggesting a split."""
    if not eligible(doc):
        raise SplitError("Only freight invoices that are in a shipment can be shared or not.")
    rows = allocations(doc)
    _check_open(doc, rows)
    si = SharedInvoice.objects.filter(document=doc).first() or SharedInvoice(organization=doc.organization,
                                                                               document=doc)
    changed = _drop(doc, si, rows, delete_record=False)
    si.status, si.note, si.updated_by, si.confirmed_hash = SharedInvoice.Status.DISMISSED, note[:500], user, ""
    si.save()
    audit(doc.organization, "shared_invoice.dismissed", doc, actor=user, invoice=invoice_number(doc), note=note,
          shipments=sorted(Shipment.objects.filter(pk__in=changed).values_list("reference", flat=True)))
    revalidate(changed | ({doc.match.shipment_id} if hasattr(doc, "match") else set()))
    return changed


@transaction.atomic
def reset(doc: Document, user) -> set[int]:
    """Back to the automatic split (after 'Not shared' or a split typed by hand)."""
    if not eligible(doc):
        raise SplitError("Only freight invoices that are in a shipment can be split.")
    rows = allocations(doc)
    _check_open(doc, rows)
    si = SharedInvoice.objects.filter(document=doc).first()
    if si:
        si.delete()  # sync() starts again from what the invoice names
    InvoiceAllocation.objects.filter(document=doc).delete()
    changed = {r.shipment_id for r in rows} | sync(doc)
    audit(doc.organization, "shared_invoice.reset", doc, actor=user, invoice=invoice_number(doc))
    revalidate(changed | ({doc.match.shipment_id} if hasattr(doc, "match") else set()))
    return changed
