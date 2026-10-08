"""Landed cost: what each product of a shipment really cost once every charge is added.

Products are the lines of the shipment's commercial invoices. Charges come from the charge sources
(freight invoices, credit notes, this shipment's share of shared invoices, and any source another app
registers, such as customs duties). Every charge is converted to the home currency
(Organization.to_home) and spread over the products in whole cents with the largest-remainder method,
so the amounts spread always add up to the charge exactly.

How a charge is spread (its basis), in order:
  1. the method chosen for its type of charge (freight, duties and taxes, insurance, handling, other),
     on the shipment if it has its own setting, else the organization's;
  2. the basis the charge source suggests (duties are usually assessed on value);
  3. the shipment's or organization's method (by value unless changed).
When a basis can't be used (weights missing on some products, no quantities), the charge is spread
by value instead (then quantity, then equally), and the page says so.

A commercial invoice line without a quantity that reads like a charge ("Freight", "Insurance"), or a
negative line (a discount), is treated as a charge on the goods rather than as a product.

Approved shipments keep the result frozen at approval (LandedCostRun), with the exchange rates of that
day, so the per-product report doesn't move when rates or settings change later.
"""
from __future__ import annotations

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from apps.accounting.models import vendor_key
from apps.documents.models import Document
from apps.documents.services.normalize import norm_ref, parse_date
from apps.shipments.models import Shipment

from ..models import Category, LandedCostLine, LandedCostRun, LandedSettings, Method, ShipmentLandedOverride
from .charges import Charge, Note, collect
from .rounding import CENT, from_cents, split, to_cents

log = logging.getLogger(__name__)
UNIT = Decimal("0.0001")
PCT = Decimal("0.1")
METHOD_WORDS = {"value": "value", "quantity": "quantity", "weight": "weight", "volume": "volume", "equal": "equally"}
CATEGORY_ORDER = [c for c, _ in Category.choices]
CATEGORY_WORDS = {"freight": "freight", "duty": "duty and tax", "insurance": "insurance", "handling": "handling",
                  "other": "other"}


def _dec(value, places=CENT) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "")).quantize(places) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _measure(value) -> Decimal | None:
    try:
        v = Decimal(str(value).replace(",", "")) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None
    return v if v is not None and v >= 0 else None


# --------------------------------------------------------------------------- policy


@dataclass
class Policy:
    method: str = Method.VALUE
    by_category: dict = field(default_factory=dict)
    source: str = "organization"     # organization | shipment

    def basis_for(self, charge: Charge) -> str:
        return self.by_category.get(charge.category) or charge.basis or self.method

    def describe(self) -> str:
        text = f"by {METHOD_WORDS.get(self.method, self.method)}"
        extra = [f"{Category(c).label.lower()} by {METHOD_WORDS.get(m, m)}" for c, m in self.by_category.items()
                 if c in Category.values and m in Method.values]
        return text + (f", except {', '.join(extra)}" if extra else "")

    def as_dict(self) -> dict:
        return {"method": self.method, "by_category": self.by_category, "source": self.source}


def clean_by_category(raw: dict | None) -> dict:
    return {c: m for c, m in (raw or {}).items() if c in Category.values and m in Method.values}


def policy_for(shipment: Shipment) -> Policy:
    override = ShipmentLandedOverride.objects.filter(shipment=shipment).first()
    if override:
        return Policy(override.method, clean_by_category(override.by_category), "shipment")
    s = LandedSettings.for_org(shipment.organization)
    return Policy(s.method, clean_by_category(s.by_category), "organization")


# --------------------------------------------------------------------------- result


@dataclass
class Product:
    position: int
    description: str
    quantity: Decimal | None = None
    value: Decimal = Decimal("0.00")           # as invoiced
    currency: str = ""
    value_home: Decimal | None = None
    sku: str = ""
    hs_code: str = ""
    weight_kg: Decimal | None = None
    volume_cbm: Decimal | None = None
    vendor_name: str = ""
    invoice_number: str = ""
    document_id: int | None = None
    allocated: dict = field(default_factory=dict)   # category -> Decimal (home currency)

    @property
    def charges(self) -> Decimal:
        return sum(self.allocated.values(), Decimal("0.00"))

    @property
    def landed(self) -> Decimal | None:
        return None if self.value_home is None else self.value_home + self.charges

    @property
    def per_unit(self) -> Decimal | None:
        if self.landed is None or not self.quantity:
            return None
        return (self.landed / self.quantity).quantize(UNIT, rounding=ROUND_HALF_UP)

    @property
    def goods_per_unit(self) -> Decimal | None:
        if self.value_home is None or not self.quantity:
            return None
        return (self.value_home / self.quantity).quantize(UNIT, rounding=ROUND_HALF_UP)

    @property
    def uplift(self) -> Decimal | None:
        if not self.value_home:
            return None
        return (self.charges / self.value_home * 100).quantize(PCT, rounding=ROUND_HALF_UP)

    @property
    def product_key(self) -> str:
        what = norm_ref(self.sku) if self.sku else "d:" + re.sub(r"\s+", " ", self.description.lower()).strip()
        return f"{vendor_key(self.vendor_name)}|{what}"[:260]

    def part(self, category: str) -> Decimal:
        return self.allocated.get(category, Decimal("0.00"))


@dataclass
class ChargeRow:
    charge: Charge
    amount_home: Decimal | None
    basis: str = ""     # what the policy asked for
    used: str = ""      # what was used after fallbacks

    @property
    def fell_back(self) -> bool:
        return bool(self.used) and self.used != self.basis


@dataclass
class LandedCost:
    shipment: Shipment
    currency: str
    policy: Policy
    as_of: date
    products: list[Product] = field(default_factory=list)
    charges: list[ChargeRow] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    complete: bool = True
    frozen_at: object = None

    @property
    def goods_total(self) -> Decimal:
        return sum((p.value_home or Decimal("0.00") for p in self.products), Decimal("0.00"))

    @property
    def charges_total(self) -> Decimal:
        return sum((p.charges for p in self.products), Decimal("0.00"))

    @property
    def landed_total(self) -> Decimal:
        return self.goods_total + self.charges_total

    @property
    def uplift(self) -> Decimal | None:
        if not self.goods_total:
            return None
        return (self.charges_total / self.goods_total * 100).quantize(PCT, rounding=ROUND_HALF_UP)

    @property
    def categories(self) -> list[tuple[str, str]]:
        used = {c for p in self.products for c, v in p.allocated.items() if v}
        return [(c, Category(c).label) for c in CATEGORY_ORDER if c in used]

    def category_total(self, category: str) -> Decimal:
        return sum((p.part(category) for p in self.products), Decimal("0.00"))

    @property
    def warnings(self) -> list[Note]:
        return [n for n in self.notes if n.level == "warning"]


# --------------------------------------------------------------------------- products


def _goods(shipment: Shipment, notes: list[Note]) -> tuple[list[Product], list[Charge]]:
    from apps.rates.charges import match_keywords

    org = shipment.organization
    products, charges = [], []
    invoices = [d for d in shipment.documents.prefetch_related("fields")
                if d.doc_type == Document.DocType.COMMERCIAL_INVOICE]
    looked_like_charges = []
    for doc in invoices:
        data = doc.data()
        cur = (data.get("currency") or org.home_currency).upper()[:3]
        number = str(data.get("invoice_number") or doc.original_filename)
        items = [i for i in data.get("line_items") or [] if isinstance(i, dict)]
        if not items:
            notes.append(Note(f"Commercial invoice {number} has no product lines, so its products can't be shown. "
                              "Check that the PDF lists them.", "warning"))
            continue
        for item in items:
            amount = _dec(item.get("amount"))
            desc = str(item.get("description") or "").strip() or "Product"
            if amount is None:
                notes.append(Note(f"“{desc[:60]}” on commercial invoice {number} has no amount, so it is left out.",
                                  "warning"))
                continue
            qty = _measure(item.get("quantity"))
            code = match_keywords(desc) if qty is None else None
            if qty is None and (code or amount < 0):
                charges.append(Charge(code=code or "other", amount=amount, currency=cur, description=desc,
                                      source=f"Commercial invoice {number}", basis=Method.VALUE,
                                      document_id=doc.pk))
                looked_like_charges.append(desc)
                continue
            products.append(Product(
                position=len(products), description=desc[:300], quantity=qty, value=amount, currency=cur,
                sku=str(item.get("sku") or "").strip()[:60], hs_code=str(item.get("hs_code") or "").strip()[:20],
                weight_kg=_measure(item.get("weight_kg")), volume_cbm=_measure(item.get("volume_cbm")),
                vendor_name=str(data.get("vendor_name") or "")[:200], invoice_number=number, document_id=doc.pk))
    if looked_like_charges:
        names = ", ".join(f"“{d[:40]}”" for d in looked_like_charges[:3]) + (" and more" if len(looked_like_charges) > 3
                                                                            else "")
        notes.append(Note(f"{names} on the commercial invoice {'has' if len(looked_like_charges) == 1 else 'have'} no "
                          "quantity and read like a charge or discount, so they are spread over the products "
                          "instead of counted as products."))
    return products, charges


def as_of(shipment: Shipment) -> date:
    """The date the report files a shipment under: the goods' invoice date, else the B/L date."""
    first_bl = None
    for d in shipment.documents.prefetch_related("fields").order_by("received_at"):
        if d.doc_type == Document.DocType.COMMERCIAL_INVOICE and parse_date(d.field("invoice_date")):
            return parse_date(d.field("invoice_date"))
        if d.doc_type == Document.DocType.BILL_OF_LADING and not first_bl:
            first_bl = parse_date(d.field("issue_date"))
    return first_bl or timezone.localdate(shipment.created_at)


# --------------------------------------------------------------------------- spreading


def _weights(basis: str, products: list[Product]) -> tuple[list[Decimal] | None, str]:
    """Weights per product for a basis, or None and the reason it can't be used."""
    n = len(products)
    if basis == "equal":
        return [Decimal(1)] * n, ""
    if basis == Method.VALUE:
        values = [max(p.value_home or Decimal(0), Decimal(0)) for p in products]
        return (values, "") if sum(values) > 0 else (None, "the products have no value")
    attr = {Method.QUANTITY: "quantity", Method.WEIGHT: "weight_kg", Method.VOLUME: "volume_cbm"}[basis]
    word = {Method.QUANTITY: "Quantity", Method.WEIGHT: "Weight", Method.VOLUME: "Volume"}[basis]
    values = [getattr(p, attr) for p in products]
    missing = sum(1 for v in values if v is None)
    if missing:
        return None, (f"{word} is missing on {missing} of {n} product{'s' if n != 1 else ''}" if missing < n
                      else f"{word} is missing on the commercial invoice")
    if sum(values) <= 0:
        return None, f"{word} is zero on every product"
    return values, ""


FALLBACKS = {
    Method.VALUE: [Method.VALUE, Method.QUANTITY, "equal"],
    Method.QUANTITY: [Method.QUANTITY, Method.VALUE, "equal"],
    Method.WEIGHT: [Method.WEIGHT, Method.VALUE, Method.QUANTITY, "equal"],
    Method.VOLUME: [Method.VOLUME, Method.VALUE, Method.QUANTITY, "equal"],
}


def spread(products: list[Product], charge_rows: list[ChargeRow], notes: list[Note]) -> None:
    """Spread every charge over the products in cents; the parts of each charge add up to it exactly."""
    fallback_notes: OrderedDict[tuple, list[str]] = OrderedDict()
    for row in charge_rows:
        if row.amount_home is None:
            continue
        reasons = []
        weights = None
        for basis in FALLBACKS.get(row.basis, [row.basis, Method.VALUE, Method.QUANTITY, "equal"]):
            weights, why = _weights(basis, products)
            if weights is not None:
                row.used = basis
                break
            reasons.append(why)
        if row.fell_back:
            key = (row.basis, row.used, reasons[0])
            fallback_notes.setdefault(key, [])
            cat = CATEGORY_WORDS.get(row.charge.category, "other")
            if cat not in fallback_notes[key]:
                fallback_notes[key].append(cat)
        for product, cents in zip(products, split(to_cents(row.amount_home), weights)):
            if cents:
                cat = row.charge.category
                product.allocated[cat] = product.allocated.get(cat, Decimal("0.00")) + from_cents(cents)
    for (asked, used, why), cats in fallback_notes.items():
        what = " and ".join(cats) if len(cats) <= 2 else ", ".join(cats[:-1]) + " and " + cats[-1]
        how = "equally" if used == "equal" else "by " + METHOD_WORDS[used]
        notes.append(Note(f"{why}, so the {what} charges were spread {how} instead of by "
                          f"{METHOD_WORDS.get(asked, asked)}.", "warning"))


# --------------------------------------------------------------------------- compute


def compute(shipment: Shipment, policy: Policy | None = None) -> LandedCost:
    org = shipment.organization
    notes: list[Note] = []
    policy = policy or policy_for(shipment)
    result = LandedCost(shipment=shipment, currency=org.home_currency, policy=policy, as_of=as_of(shipment))
    products, goods_charges = _goods(shipment, notes)
    charges, source_notes = collect(shipment)
    charges = goods_charges + charges
    notes += source_notes
    missing_rates = set()
    for p in products:
        p.value_home = org.to_home(p.value, p.currency)
        if p.value_home is None:
            missing_rates.add(p.currency)
    rows = []
    for c in charges:
        home = org.to_home(c.amount, c.currency)
        if home is None:
            missing_rates.add(c.currency)
        rows.append(ChargeRow(c, home, basis=policy.basis_for(c)))
    result.products, result.charges, result.notes = products, rows, notes
    if not products:
        result.complete = False
        if not any(d.doc_type == Document.DocType.COMMERCIAL_INVOICE for d in shipment.documents):
            notes.insert(0, Note("No commercial invoice in this shipment yet, so there are no products to spread the "
                                 "charges over. Landed cost appears when the supplier's invoice arrives.", "warning"))
        return result
    if missing_rates:
        result.complete = False
        cur = ", ".join(sorted(missing_rates))
        notes.insert(0, Note(f"No exchange rate for {cur}, so landed cost can't be worked out in {org.home_currency}. "
                             "An admin can add the rate under Settings, Organization.", "warning"))
        return result
    if not rows:
        notes.append(Note("No freight, duty or other charges in this shipment yet, so landed cost equals the goods "
                          "value."))
    spread(products, rows, notes)
    return result


# --------------------------------------------------------------------------- frozen at approval


@transaction.atomic
def freeze(shipment: Shipment) -> LandedCostRun:
    """Store the shipment's landed cost as it is now (called when it is approved)."""
    lc = compute(shipment)
    LandedCostRun.objects.filter(shipment=shipment).delete()
    run = LandedCostRun.objects.create(
        organization=shipment.organization, shipment=shipment, as_of=lc.as_of, currency=lc.currency,
        goods_total=lc.goods_total, charges_total=lc.charges_total, landed_total=lc.landed_total,
        complete=lc.complete, policy=lc.policy.as_dict(), notes=[{"text": n.text, "level": n.level} for n in lc.notes],
        charges=[{"source": r.charge.source, "description": r.charge.description, "code": r.charge.code,
                  "category": r.charge.category, "amount": f"{r.charge.amount:.2f}", "currency": r.charge.currency,
                  "amount_home": f"{r.amount_home:.2f}" if r.amount_home is not None else None, "basis": r.basis,
                  "used": r.used, "shared": r.charge.shared, "document_id": r.charge.document_id}
                 for r in lc.charges])
    LandedCostLine.objects.bulk_create([LandedCostLine(
        run=run, organization=shipment.organization, shipment=shipment, position=p.position,
        product_key=p.product_key, sku=p.sku, description=p.description, hs_code=p.hs_code,
        vendor_name=p.vendor_name, quantity=p.quantity, weight_kg=p.weight_kg, volume_cbm=p.volume_cbm,
        goods_value=p.value_home or Decimal("0.00"), charges=p.charges,
        by_category={k: f"{v:.2f}" for k, v in p.allocated.items() if v}, landed_total=p.landed or Decimal("0.00"),
        per_unit=p.per_unit, as_of=lc.as_of) for p in lc.products] if lc.complete else [])
    return run


def unfreeze(shipment: Shipment) -> None:
    LandedCostRun.objects.filter(shipment=shipment).delete()


def from_run(run: LandedCostRun) -> LandedCost:
    pol = run.policy or {}
    lc = LandedCost(shipment=run.shipment, currency=run.currency, as_of=run.as_of, complete=run.complete,
                    policy=Policy(pol.get("method", Method.VALUE), pol.get("by_category") or {},
                                  pol.get("source", "organization")),
                    notes=[Note(n.get("text", ""), n.get("level", "info")) for n in run.notes or []],
                    frozen_at=run.computed_at)
    for line in run.lines.all():
        lc.products.append(Product(
            position=line.position, description=line.description, quantity=line.quantity, value=line.goods_value,
            currency=run.currency, value_home=line.goods_value, sku=line.sku, hs_code=line.hs_code,
            weight_kg=line.weight_kg, volume_cbm=line.volume_cbm, vendor_name=line.vendor_name,
            allocated={k: Decimal(v) for k, v in (line.by_category or {}).items()}))
    for c in run.charges or []:
        charge = Charge(code=c.get("code") or "other", amount=Decimal(c.get("amount") or "0"),
                        currency=c.get("currency") or run.currency, description=c.get("description") or "",
                        source=c.get("source") or "", category=c.get("category") or "", shared=bool(c.get("shared")),
                        document_id=c.get("document_id"))
        home = c.get("amount_home")
        lc.charges.append(ChargeRow(charge, Decimal(home) if home is not None else None, c.get("basis") or "",
                                    c.get("used") or ""))
    return lc


def ensure_run(shipment: Shipment) -> LandedCostRun:
    run = LandedCostRun.objects.filter(shipment=shipment).first()
    return run or freeze(shipment)


def landed_for(shipment: Shipment) -> LandedCost:
    """Frozen result for approved and posted shipments, live result for the others."""
    if shipment.is_locked:
        try:
            return from_run(ensure_run(shipment))
        except Exception:  # never break the shipment page; show the live figures instead
            log.exception("Could not freeze landed cost for shipment %s", shipment.pk)
    return compute(shipment)
