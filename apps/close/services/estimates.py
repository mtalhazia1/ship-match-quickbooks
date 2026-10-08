"""Estimate a charge group that hasn't been invoiced yet.

Tried in this order; the first that gives an amount wins:

1. Quote: a quote on file (apps/rates) from a likely vendor that fits the shipment's lane, date and
   equipment and lists charges in this group. Per-container charges count the shipment's containers;
   per-shipment and per-B/L charges count once; per-kg, per-cbm, per-day and per-hour charges can't be
   counted before the invoice and are left out (said in the basis).
2. Vendor median: the median cost per container of the likely vendor's past invoices for this group on the
   same lane (port of loading and port of discharge) and equipment.
3. Organization median: the median cost per container of every past invoice for this group.

Likely vendors, most likely first: vendors already billing this shipment who have billed this group
before; vendors that bill this group at the same port of discharge; vendors that bill it anywhere; vendors
with quotes listing charges in this group.

Confidence (0 to 1) says how much to trust the number: a quote that fits without assumptions 0.9; lower with
each assumption (lane or equipment not printed, container count unknown, and most of all a vendor that isn't
billing the shipment yet); a vendor median 0.65 (0.55 when past invoices vary a lot, less again when the vendor
is a guess); an organization median 0.35. Labels: 0.8 and up high, 0.55 and up medium, below that low.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median

from apps.rates import charges as charge_codes
from apps.rates.matching import find_quote
from apps.rates.models import Quote, QuoteCharge

from .. import groups
from .history import CENT, History, ShipmentFacts

QUOTE, VENDOR_MEDIAN, ORG_MEDIAN, MANUAL, NONE, RECEIVED = (
    "quote", "vendor_median", "org_median", "manual", "none", "received")
METHOD_LABELS = {
    RECEIVED: "Invoice received",
    QUOTE: "Quote on file",
    VENDOR_MEDIAN: "Vendor's usual cost on this lane",
    ORG_MEDIAN: "Your usual cost per container",
    MANUAL: "Entered by a person",
    NONE: "No basis to estimate",
}
COUNTED = {QuoteCharge.Basis.CONTAINER, QuoteCharge.Basis.SHIPMENT, QuoteCharge.Basis.BL}
# How much less to trust an estimate when the vendor is a guess, by how the vendor was chosen.
VENDOR_GUESS = {"usual_here": 0.15, "usual": 0.2, "quotes": 0.25}
WHY_TEXT = {"usual_here": "it usually bills this charge at this port",
            "usual": "it bills this charge most often", "quotes": "its quote fits"}


def confidence_label(value: float | None, method: str = "") -> str:
    if method == RECEIVED:
        return "Actual"
    if method == MANUAL:
        return "Set by a person"
    if value is None or method == NONE:
        return "None"
    if value >= 0.8:
        return "High"
    if value >= 0.55:
        return "Medium"
    return "Low"


@dataclass
class Estimate:
    method: str
    amount_home: Decimal | None = None
    amount: Decimal | None = None
    currency: str = ""
    vendor_key: str = ""
    vendor_name: str = ""
    confidence: float = 0.0
    basis: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    vendor_key: str
    vendor_name: str
    why: str          # on_shipment | usual_here | usual | quotes


def candidates(org, facts: ShipmentFacts, group: str, history: History) -> list[Candidate]:
    out: dict[str, Candidate] = {}
    billed = {vk for vk, _, _ in history.vendor_counts(group)}
    for vk, name in facts.vendors.items():
        if vk in billed:
            out.setdefault(vk, Candidate(vk, name, "on_shipment"))
    if facts.destination_key:
        for vk, name, _ in history.vendor_counts(group, facts.destination_key):
            out.setdefault(vk, Candidate(vk, name, "usual_here"))
    for vk, name, _ in history.vendor_counts(group):
        out.setdefault(vk, Candidate(vk, name, "usual"))
    codes = groups.codes_in(group)
    on_shipment = set(facts.vendors)
    quoted = (Quote.objects.filter(organization=org, archived=False, charges__code__in=codes)
              .values_list("vendor_key", "vendor_name").distinct().order_by("vendor_name"))
    for vk, name in sorted(quoted, key=lambda t: (t[0] not in on_shipment, t[1])):
        out.setdefault(vk, Candidate(vk, facts.vendors.get(vk, name), "on_shipment" if vk in on_shipment else "quotes"))
    return list(out.values())


def estimate(org, facts: ShipmentFacts, group: str, history: History, min_history: int) -> Estimate:
    cands = candidates(org, facts, group, history)
    notes: list[str] = []
    n, assumed_boxes = (facts.containers, False) if facts.containers else (1, True)

    found = _from_quote(org, facts, group, cands, n, assumed_boxes, notes)
    if found:
        return found
    found = _from_vendor_median(facts, group, cands, history, min_history, n, assumed_boxes, notes)
    if found:
        return found
    found = _from_org_median(group, cands, history, min_history, n, assumed_boxes, notes)
    if found:
        return found
    first = cands[0] if cands else None
    notes.append("No quote fits and there are too few past invoices for this charge to estimate it. "
                 "Enter an amount, or mark it as not needed.")
    return Estimate(NONE, vendor_key=first.vendor_key if first else "", vendor_name=first.vendor_name if first else "",
                    basis=" ".join(notes), notes=notes)


def _lower(text: str) -> str:
    """'Security filing (ISF, AMS, ENS)' -> 'security filing (ISF, AMS, ENS)'; acronyms keep their capitals."""
    return text[:1].lower() + text[1:] if text[1:2].islower() else text


def _boxes(n: int, assumed: bool) -> str:
    return f"{n} container{'s' if n != 1 else ''}" + (" (count not known, 1 assumed)" if assumed else "")


def _from_quote(org, facts, group, cands, n, assumed_boxes, notes) -> Estimate | None:
    if facts.ctx is None or not cands:
        return None
    codes = groups.codes_in(group)
    ctx = facts.ctx
    if facts.ship_date:
        ctx.ship_date, ctx.date_source = facts.ship_date, facts.date_source
    for cand in cands:
        match = find_quote(org, cand.vendor_key, ctx)
        if match.status != "matched" or match.quote is None:
            continue
        quote = match.quote
        lines = [qc for qc in quote.charges.all() if qc.code in codes]
        if not lines:
            continue
        total, parts, skipped = Decimal("0.00"), [], []
        for qc in lines:
            if qc.basis not in COUNTED:
                skipped.append(f"{_lower(charge_codes.label(qc.code))} ({qc.get_basis_display().lower()})")
                continue
            count = n if qc.basis == QuoteCharge.Basis.CONTAINER else 1
            total += (qc.amount * count).quantize(CENT)
            what = _lower(charge_codes.label(qc.code))
            parts.append(f"{what} {qc.amount:,.2f} x {count}" if qc.basis == QuoteCharge.Basis.CONTAINER
                         else f"{what} {qc.amount:,.2f} {qc.get_basis_display().lower()}")
        if not parts:
            continue
        home = org.to_home(total, quote.currency)
        if home is None:
            notes.append(f"{quote.title} is in {quote.currency} and there is no exchange rate for it in Settings.")
            continue
        conf = 0.9
        assumptions = list(match.assumed)
        if assumed_boxes:
            conf -= 0.1
            assumptions.append("container count not known, 1 assumed")
        if match.assumed:
            conf -= 0.15
        if cand.why != "on_shipment":
            conf -= VENDOR_GUESS[cand.why]
            assumptions.append(f"{cand.vendor_name} isn't billing this shipment yet; {WHY_TEXT[cand.why]}")
        basis = (f"{quote.title} from {quote.vendor_name} ({quote.lane}"
                 + (f", {quote.equipment}" if quote.equipment else "") + f"): {', '.join(parts)}"
                 + (f" for {_boxes(n, False)}" if any(qc.basis == QuoteCharge.Basis.CONTAINER for qc in lines) else ""))
        if quote.currency != org.home_currency:
            basis += f", {quote.currency} {total:,.2f} converted"
        if skipped:
            basis += f". Not included because they can't be counted before the invoice: {', '.join(skipped)}"
        if assumptions:
            basis += f". Assumed: {'; '.join(assumptions)}"
        return Estimate(QUOTE, amount_home=home, amount=total, currency=quote.currency, vendor_key=cand.vendor_key,
                        vendor_name=cand.vendor_name, confidence=round(max(conf, 0.5), 2), basis=basis + ".",
                        notes=notes)
    return None


def _spread(values: list[Decimal]) -> Decimal:
    low = min(values)
    return (max(values) / low) if low > 0 else Decimal("99")


def _from_vendor_median(facts, group, cands, history, min_history, n, assumed_boxes, notes) -> Estimate | None:
    if not facts.lane_known:
        notes.append("The lane isn't known (no bill of lading with ports), so past invoices on the same lane "
                     "can't be used.")
        return None
    for cand in cands:
        if cand.why == "quotes":
            continue
        same = [s for s in history.for_group(group)
                if s.vendor_key == cand.vendor_key and s.origin_key == facts.origin_key
                and s.destination_key == facts.destination_key
                and (not facts.equipment or not s.equipment or s.equipment == facts.equipment)]
        if len(same) < min_history:
            continue
        per = [s.per_container for s in same]
        typical = Decimal(str(median(per))).quantize(CENT)
        amount = (typical * n).quantize(CENT)
        conf = 0.65 if _spread(per) <= Decimal("1.5") else 0.55
        if assumed_boxes:
            conf -= 0.1
        if cand.why != "on_shipment":
            conf -= 0.1
        eq = f" {facts.equipment}" if facts.equipment else ""
        basis = (f"Median of {len(same)} past shipments billed by {cand.vendor_name} for this charge on "
                 f"{facts.lane_label}{eq}: {typical:,.2f} per container x {_boxes(n, assumed_boxes)}"
                 f" (range {min(per):,.2f} to {max(per):,.2f}).")
        if not facts.equipment:
            basis += " Equipment type not printed, so every equipment type on this lane was used."
        if cand.why != "on_shipment":
            basis += f" {cand.vendor_name} isn't billing this shipment yet; {WHY_TEXT[cand.why]}."
        return Estimate(VENDOR_MEDIAN, amount_home=amount, amount=amount, currency="", vendor_key=cand.vendor_key,
                        vendor_name=cand.vendor_name, confidence=round(conf, 2), basis=basis, notes=notes)
    return None


def _from_org_median(group, cands, history, min_history, n, assumed_boxes, notes) -> Estimate | None:
    samples = history.for_group(group, per="shipment")
    if len(samples) < min_history:
        return None
    per = [s.per_container for s in samples]
    typical = Decimal(str(median(per))).quantize(CENT)
    amount = (typical * n).quantize(CENT)
    conf = 0.35 if _spread(per) <= Decimal("2") else 0.3
    if assumed_boxes:
        conf -= 0.05
    first = next((c for c in cands if c.why != "quotes"), cands[0] if cands else None)
    basis = (f"Median of {len(samples)} past shipments billed for {groups.label(group).lower()}, any vendor and lane: "
             f"{typical:,.2f} per container x {_boxes(n, assumed_boxes)}.")
    if first:
        basis += f" Likely vendor: {first.vendor_name}."
    return Estimate(ORG_MEDIAN, amount_home=amount, amount=amount, currency="",
                    vendor_key=first.vendor_key if first else "", vendor_name=first.vendor_name if first else "",
                    confidence=round(conf, 2), basis=basis, notes=notes)
