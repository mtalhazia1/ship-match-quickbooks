"""What ShipMatch knows about every shipment and what each charge group usually costs.

Built once per accrual report from the organization's documents:

* `ShipmentFacts`: when a shipment shipped (on-board date printed on the bill of lading, else the B/L issue
  date), its lane and equipment (the same reading the rate checks use), its container count, and which
  charge groups its freight invoices already bill.
* `History`: one sample per invoice and charge group (vendor, lane, equipment, containers, amount in the
  home currency), for medians.

Invoices that are open possible duplicates, and shipments that were rejected, are left out of both.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from apps.accounting.models import vendor_key
from apps.documents.models import Document, ExtractedField
from apps.documents.services.normalize import parse_date
from apps.rates import charges, lanes
from apps.rates.matching import ShipmentContext, context_for
from apps.shipments.models import Shipment, ValidationIssue

from .. import groups

CENT = Decimal("0.01")
DUPLICATE_CODES = ("duplicate_invoice", "duplicate_credit_note")
_ON_BOARD = re.compile(r"(?:shipped\s+)?on[\s-]*board(?:\s+date)?", re.I)
_DATE_SHAPES = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/.]\d{1,2}[/.]\d{4}|\d{1,2}[ -](?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"[a-z]*[ -]\d{2,4}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]* \d{1,2},? \d{4})\b", re.I)


def dec(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(CENT) if value not in (None, "") else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def on_board_date(text: str) -> date | None:
    """The 'shipped on board' date printed on a bill of lading, if any."""
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        m = _ON_BOARD.search(line)
        if not m:
            continue
        rest = line[m.end():]
        # The date follows the words on the same line, or on the next line when the stamp ends with them.
        chunks = [rest] + ([lines[i + 1]] if i + 1 < len(lines) and not re.search(r"[A-Za-z]{3,}", rest) else [])
        for chunk in chunks:
            for found in _DATE_SHAPES.finditer(chunk):
                d = parse_date(found.group(1).replace("Sept", "Sep").replace("sept", "sep"))
                if d:
                    return d
    return None


@dataclass
class InvoicePart:
    document: Document
    group: str
    amount: Decimal              # in the invoice's currency
    currency: str
    amount_home: Decimal | None


@dataclass
class ShipmentFacts:
    shipment: Shipment
    docs: list[Document]
    ship_date: date | None = None
    date_source: str = ""
    ctx: ShipmentContext | None = None
    origin_key: str = ""
    destination_key: str = ""
    equipment: str = ""
    containers: int = 0
    parts: list[InvoicePart] = field(default_factory=list)
    vendors: dict[str, str] = field(default_factory=dict)   # vendor_key -> name, freight invoices on it

    @property
    def invoiced_groups(self) -> set[str]:
        return {p.group for p in self.parts}

    @property
    def lane_known(self) -> bool:
        return bool(self.origin_key and self.destination_key)

    @property
    def lane_label(self) -> str:
        if not self.ctx:
            return "unknown lane"
        return f"{lanes.describe(self.ctx.origin or '') or 'unknown origin'} to " \
               f"{lanes.describe(self.ctx.destination or '') or 'unknown destination'}"


@dataclass
class Sample:
    vendor_key: str
    vendor_name: str
    group: str
    origin_key: str
    destination_key: str
    equipment: str
    containers: int
    amount_home: Decimal
    shipment_id: int
    document_id: int

    @property
    def per_container(self) -> Decimal:
        return (self.amount_home / max(1, self.containers)).quantize(CENT)


@dataclass
class History:
    samples: list[Sample] = field(default_factory=list)
    shipments_by_dest: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    group_shipments_by_dest: dict[tuple[str, str], set[int]] = field(default_factory=lambda: defaultdict(set))
    all_shipments: set[int] = field(default_factory=set)
    group_shipments: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    samples_by_group: dict[str, list[Sample]] = field(default_factory=lambda: defaultdict(list))
    vendor_group_counts: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))

    def add(self, facts: ShipmentFacts, sample: Sample) -> None:
        self.samples.append(sample)
        self.samples_by_group[sample.group].append(sample)           # indexes, so lookups don't rescan every sample
        self.vendor_group_counts[sample.vendor_key][sample.group] += 1
        self.group_shipments_by_dest[(facts.destination_key, sample.group)].add(facts.shipment.pk)
        self.group_shipments[sample.group].add(facts.shipment.pk)

    def for_group(self, group: str, per: str = "vendor") -> list[Sample]:
        """What each shipment cost for this group: per shipment and vendor (two invoices from one vendor for the
        same group count as one), or per shipment across vendors (per="shipment")."""
        combined: dict[tuple, Sample] = {}
        for s in self.samples_by_group.get(group, ()):
            key = (s.shipment_id, s.vendor_key) if per == "vendor" else (s.shipment_id,)
            if key in combined:
                c = combined[key]
                combined[key] = Sample(c.vendor_key, c.vendor_name, group, c.origin_key, c.destination_key,
                                       c.equipment, c.containers, c.amount_home + s.amount_home, c.shipment_id,
                                       c.document_id)
            else:
                combined[key] = s
        return list(combined.values())

    def share(self, group: str, destination_key: str, min_count: int) -> tuple[float | None, str]:
        """Share of comparable shipments billed for this group: same port of discharge when there are
        enough of them, else all the organization's shipments."""
        at_dest = self.shipments_by_dest.get(destination_key, set()) if destination_key else set()
        if destination_key and len(at_dest) >= min_count:
            n = len(self.group_shipments_by_dest.get((destination_key, group), set()))
            return n / len(at_dest), f"{n} of {len(at_dest)} shipments to this port"
        if len(self.all_shipments) >= min_count:
            n = len(self.group_shipments.get(group, set()))
            return n / len(self.all_shipments), f"{n} of {len(self.all_shipments)} shipments"
        return None, "not enough past shipments"

    def vendor_counts(self, group: str, destination_key: str = "") -> list[tuple[str, str, int]]:
        """Vendors that billed this group (at this port, when given), most frequent first."""
        counts: dict[str, list] = {}
        for s in self.for_group(group):
            if destination_key and s.destination_key != destination_key:
                continue
            entry = counts.setdefault(s.vendor_key, [s.vendor_name, 0])
            entry[1] += 1
        return sorted(((k, v[0], v[1]) for k, v in counts.items()), key=lambda t: (-t[2], t[1]))


@dataclass
class Book:
    """Everything one accrual run reads, loaded once."""

    org: object
    facts: dict[int, ShipmentFacts]
    history: History
    documents: list[Document]
    duplicates: set[int]
    rejected: set[int]


def load(org) -> Book:
    # The OCR text of a document is large and only bills of lading are read for it (below), and only the name and
    # value of each field are used here: leave the rest out of this whole-organization read.
    docs = list(Document.objects.filter(organization=org)
                .exclude(status__in=list(Document.CONTAINER_STATUSES))
                .select_related("match__shipment", "posted_bill").defer("text")
                .order_by("received_at", "id"))
    _preload_values(docs)
    bl_ids = [d.pk for d in docs if d.doc_type == Document.DocType.BILL_OF_LADING]
    if bl_ids:
        texts = dict(Document.objects.filter(pk__in=bl_ids).values_list("pk", "text"))
        for d in docs:
            if d.pk in texts:
                d.text = texts[d.pk]
    duplicates = set(ValidationIssue.objects.filter(organization=org, resolved=False, code__in=DUPLICATE_CODES,
                                                    document__isnull=False).values_list("document_id", flat=True))
    by_shipment: dict[int, list[Document]] = defaultdict(list)
    shipments: dict[int, Shipment] = {}
    for d in docs:
        link = d.match if hasattr(d, "match") else None
        if link is not None:
            by_shipment[link.shipment_id].append(d)
            shipments[link.shipment_id] = link.shipment
    rejected = {pk for pk, s in shipments.items() if s.status == Shipment.Status.REJECTED}

    facts: dict[int, ShipmentFacts] = {}
    pending_unlined: list[tuple[ShipmentFacts, Document, dict]] = []
    descriptions: list[str] = []
    invoices: list[tuple[ShipmentFacts, Document, dict]] = []
    for pk, s in shipments.items():
        f = _facts(s, by_shipment[pk])
        facts[pk] = f
        for d in f.docs:
            if d.doc_type != Document.DocType.FREIGHT_INVOICE or d.pk in duplicates:
                continue
            data = d.data()
            invoices.append((f, d, data))
            descriptions += [str((i or {}).get("description") or "").strip()
                             for i in (data.get("line_items") or []) if isinstance(i, dict)]
    codes = charges.classify_many([x for x in descriptions if x], org=org, use_ai=False) if descriptions else {}

    history = History()
    for f, d, data in invoices:
        name = (data.get("vendor_name") or "").strip()
        vk = vendor_key(name)
        if vk:
            f.vendors.setdefault(vk, name)
        cur = (data.get("currency") or org.home_currency).upper()
        amounts: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
        for item in data.get("line_items") or []:
            if not isinstance(item, dict):
                continue
            amount = dec(item.get("amount"))
            if amount is None or amount == 0:
                continue
            desc = str(item.get("description") or "").strip()
            amounts[groups.group_for(codes.get(desc, "other") if desc else "other")] += amount
        total = dec(data.get("total_amount"))
        if not amounts:
            if total is not None:
                pending_unlined.append((f, d, data))
            continue
        if total is not None:  # an accepted total that differs from the lines: the difference sits with the largest part
            gap = total - sum(amounts.values())
            if gap and abs(gap) <= abs(total):
                biggest = max(amounts, key=lambda g: abs(amounts[g]))
                amounts[biggest] += gap
        for group, amount in amounts.items():
            _add_part(org, history, f, d, vk, name, group, amount, cur, rejected)

    # Invoices without readable lines: the whole total goes to the group this vendor usually bills.
    for f, d, data in pending_unlined:
        name = (data.get("vendor_name") or "").strip()
        vk = vendor_key(name)
        usual = _usual_group(history, vk)
        cur = (data.get("currency") or org.home_currency).upper()
        _add_part(org, history, f, d, vk, name, usual, dec(data.get("total_amount")), cur, rejected)

    for f in facts.values():
        if f.shipment.pk in rejected or not f.parts:
            continue
        history.all_shipments.add(f.shipment.pk)
        if f.destination_key:
            history.shipments_by_dest[f.destination_key].add(f.shipment.pk)
    return Book(org=org, facts=facts, history=history, documents=docs, duplicates=duplicates, rejected=rejected)


def _preload_values(docs: list[Document]) -> None:
    """Give each document its extracted values from one plain query (name and value only), instead of building a
    field object for every value: a whole-organization read has tens of thousands of them."""
    values: dict[int, dict] = {d.pk: {} for d in docs}
    ids = list(values)
    for start in range(0, len(ids), 5000):
        for doc_id, name, value in (ExtractedField.objects.filter(document_id__in=ids[start:start + 5000])
                                    .order_by("id").values_list("document_id", "name", "value")):
            values[doc_id][name] = value
    for d in docs:
        d._preloaded_values = values[d.pk]


def _usual_group(history: History, vk: str) -> str:
    counts = history.vendor_group_counts.get(vk)
    return max(counts, key=counts.get) if counts else groups.FREIGHT


def _add_part(org, history: History, f: ShipmentFacts, d: Document, vk: str, name: str, group: str,
              amount: Decimal, cur: str, rejected: set[int]) -> None:
    home = org.to_home(amount, cur)
    f.parts.append(InvoicePart(d, group, amount, cur, home))
    if f.shipment.pk in rejected or home is None or not vk:
        return
    history.add(f, Sample(vk, name, group, f.origin_key, f.destination_key, f.equipment, f.containers or 1,
                          home, f.shipment.pk, d.pk))


def _facts(shipment: Shipment, docs: list[Document]) -> ShipmentFacts:
    f = ShipmentFacts(shipment=shipment, docs=docs)
    bls = [d for d in docs if d.doc_type == Document.DocType.BILL_OF_LADING]
    for bl in bls:
        boarded = on_board_date(bl.text)
        if boarded:
            f.ship_date, f.date_source = boarded, "on-board date on the bill of lading"
            break
    if not f.ship_date:
        for bl in bls:
            issued = parse_date(bl.data().get("issue_date"))
            if issued:
                f.ship_date, f.date_source = issued, "bill of lading issue date"
                break
    ctx = context_for(shipment, docs, None, {})
    f.ctx = ctx
    f.origin_key = lanes.place_key(ctx.origin) if ctx.origin else ""
    f.destination_key = lanes.place_key(ctx.destination) if ctx.destination else ""
    f.equipment = ctx.equipment or ""
    f.containers = len(shipment.container_numbers or []) or ctx.containers
    return f
