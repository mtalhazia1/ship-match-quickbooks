"""Which quote applies to a freight invoice.

The lane comes from the bill of lading in the same shipment (port of loading and port of
discharge); freight invoices don't print it. The equipment type is read from the B/L and invoice
text (e.g. "40HC" in the container table). The date is the B/L issue date, else the invoice date,
else the day the invoice was received.

Rules, in order:
  1. Only the vendor's quotes that aren't archived and are valid on that date.
  2. Origin and destination must match loosely (same UN/LOCODE, same port area, or nearly the
     same name). A quote with an empty origin or destination accepts any.
  3. Equipment must match; a quote with no equipment accepts any.
  4. The most specific quote wins (exact port over port area over "any"; named equipment over
     any), then the most recent start date, then the most recently entered.
  5. When the shipment's lane or equipment is unknown and the remaining quotes differ in it,
     no quote is chosen (the reviewer is told why) instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from django.utils import timezone

from apps.documents.models import Document
from apps.documents.services.normalize import parse_date

from . import lanes
from .models import Quote


@dataclass
class ShipmentContext:
    origin: str | None = None
    destination: str | None = None
    equipment: str | None = None
    ship_date: date | None = None
    date_source: str = ""
    containers: int = 0
    has_bl: bool = False

    @property
    def lane(self) -> str:
        return f"{self.origin or 'unknown origin'} to {self.destination or 'unknown destination'}"


def context_for(shipment, docs: list[Document], invoice: Document | None = None, data: dict | None = None
                ) -> ShipmentContext:
    """Lane, equipment, date and container count for checking one invoice in a shipment."""
    ctx = ShipmentContext()
    bls = [d for d in docs if d.doc_type == Document.DocType.BILL_OF_LADING]
    for bl in bls:
        bd = bl.data()
        ctx.has_bl = True
        ctx.origin = ctx.origin or (bd.get("port_of_loading") or None)
        ctx.destination = ctx.destination or (bd.get("port_of_discharge") or None)
        if not ctx.ship_date and bd.get("issue_date"):
            ctx.ship_date, ctx.date_source = parse_date(bd["issue_date"]), "B/L issue date"
        ctx.containers = max(ctx.containers, len(bd.get("container_numbers") or []))
    data = data if data is not None else (invoice.data() if invoice else {})
    if not ctx.ship_date and data.get("invoice_date"):
        ctx.ship_date, ctx.date_source = parse_date(data["invoice_date"]), "invoice date"
    if not ctx.ship_date and invoice is not None:
        ctx.ship_date, ctx.date_source = timezone.localdate(invoice.received_at), "date received"
    invoice_boxes = len(data.get("container_numbers") or [])
    ctx.containers = invoice_boxes or ctx.containers or len(shipment.container_numbers or [])

    # Equipment: count types written on the B/L first, then the invoice itself.
    counts: dict[str, int] = {}
    for d in bls + ([invoice] if invoice is not None else []):
        for code, n in lanes.equipment_in_text(d.text).items():
            counts[code] = counts.get(code, 0) + n
    for item in data.get("line_items") or []:
        eq = lanes.normalize_equipment(str((item or {}).get("description") or ""))
        if eq:
            counts[eq] = counts.get(eq, 0) + 1
    if len(counts) == 1:
        ctx.equipment = next(iter(counts))
    elif counts:  # mixed equipment: the most common type, if it clearly dominates
        top = sorted(counts.items(), key=lambda kv: -kv[1])
        if top[0][1] > top[1][1]:
            ctx.equipment = top[0][0]
    return ctx


@dataclass
class QuoteMatch:
    status: str                      # matched | none_on_file | no_match | ambiguous
    quote: Quote | None = None
    reason: str = ""
    candidates: list[Quote] = field(default_factory=list)
    on_file: int = 0
    assumed: list[str] = field(default_factory=list)  # e.g. "lane not confirmed (no bill of lading)"


def _score(q: Quote, ctx: ShipmentContext) -> tuple | None:
    """Specificity score for a quote that fits, None if it doesn't."""
    parts, unknown = [], []
    for quote_place, actual, name in ((q.origin, ctx.origin, "origin"), (q.destination, ctx.destination,
                                                                         "destination")):
        if not quote_place:
            parts.append(0.5)
        elif not actual:
            parts.append(0.6)
            unknown.append(name)
        else:
            s = lanes.match_score(quote_place, actual)
            if s is None:
                return None
            parts.append(s)
    if not q.equipment:
        parts.append(0.5)
    elif not ctx.equipment:
        parts.append(0.6)
        unknown.append("equipment")
    elif q.equipment == ctx.equipment:
        parts.append(1.0)
    else:
        return None
    return (sum(parts), q.valid_from, q.pk), unknown


def find_quote(org, vkey: str, ctx: ShipmentContext) -> QuoteMatch:
    quotes = list(Quote.objects.filter(organization=org, vendor_key=vkey, archived=False)
                  .prefetch_related("charges"))
    if not quotes:
        return QuoteMatch("none_on_file")
    on_date = [q for q in quotes if ctx.ship_date is None or q.is_valid_on(ctx.ship_date)]
    if not on_date:
        when = f"{ctx.ship_date.day} {ctx.ship_date:%b %Y}" if ctx.ship_date else "this date"
        return QuoteMatch("no_match", reason=f"none is valid on {when} ({ctx.date_source})", on_file=len(quotes))
    scored = []
    for q in on_date:
        res = _score(q, ctx)
        if res:
            scored.append((res[0], res[1], q))
    if not scored:
        what = ctx.lane + (f" for {ctx.equipment}" if ctx.equipment else "")
        return QuoteMatch("no_match", reason=f"none covers {what}", on_file=len(quotes))
    scored.sort(key=lambda t: t[0], reverse=True)
    best_score, best_unknown, best = scored[0]
    if best_unknown:
        # The shipment doesn't say which lane / equipment: only safe if every fitting quote agrees on it.
        rivals = [q for s, _, q in scored if q.pk != best.pk]
        differs = [dim for dim in best_unknown if any(_dim(q, dim) != _dim(best, dim) for q in rivals)]
        if differs:
            missing = " and ".join(dict.fromkeys(_missing_text(d, ctx) for d in differs))
            return QuoteMatch("ambiguous", reason=f"{len(scored)} quotes could apply, and {missing}",
                              candidates=[q for _, _, q in scored], on_file=len(quotes))
    m = QuoteMatch("matched", quote=best, on_file=len(quotes), candidates=[q for _, _, q in scored])
    if "origin" in best_unknown or "destination" in best_unknown:
        m.assumed.append("lane not confirmed because the bill of lading isn't in this shipment"
                         if not ctx.has_bl else "lane not printed on the bill of lading")
    if "equipment" in best_unknown:
        m.assumed.append("equipment type not printed on the documents")
    return m


def _dim(q: Quote, dim: str) -> str:
    if dim == "origin":
        return q.origin_key
    if dim == "destination":
        return q.destination_key
    return q.equipment


def _missing_text(dim: str, ctx: ShipmentContext) -> str:
    if dim in ("origin", "destination"):
        return ("there's no bill of lading to show the lane" if not ctx.has_bl
                else f"the bill of lading doesn't show the port of {'loading' if dim == 'origin' else 'discharge'}")
    return "the documents don't show the equipment type"
