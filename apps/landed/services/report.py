"""Landed cost per product over time, for the whole organization.

Approved and posted shipments use the landed cost frozen at approval. Shipments still in review can be
added on request; they are worked out live and marked "in review". A product is the supplier plus its
SKU (or the description when the invoice prints no SKU).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from apps.shipments.models import Shipment

from ..models import LandedCostLine, LandedCostRun
from . import landed

FREEZE_PER_VIEW = 100      # approved shipments without a frozen result, worked out per page view
LIVE_LIMIT = 150           # shipments in review worked out live per page view
PERIODS = [("90d", "Last 90 days"), ("12m", "Last 12 months"), ("year", "This year"), ("all", "All time"),
           ("custom", "Custom")]


def period_range(key: str, today: date, start: date | None = None, end: date | None = None) -> tuple[date, date]:
    if key == "90d":
        return today - timedelta(days=89), today
    if key == "year":
        return today.replace(month=1, day=1), today
    if key == "all":
        return date(2000, 1, 1), today
    if key == "custom" and start and end:
        return (start, end) if start <= end else (end, start)
    return today.replace(day=1) - timedelta(days=334), today   # 12 months, whole first month


@dataclass
class Point:
    as_of: date
    shipment_id: int
    reference: str
    quantity: Decimal | None
    goods: Decimal
    charges: Decimal
    landed: Decimal
    per_unit: Decimal | None
    in_review: bool = False


@dataclass
class ProductRow:
    key: str
    sku: str
    description: str
    vendor_name: str
    hs_code: str = ""
    points: list[Point] = field(default_factory=list)

    def _sorted(self) -> list[Point]:
        return sorted(self.points, key=lambda p: (p.as_of, p.shipment_id))

    @property
    def counted(self) -> list[Point]:
        return [p for p in self._sorted() if p.quantity]

    @property
    def quantity(self) -> Decimal:
        return sum((p.quantity or Decimal(0) for p in self.points), Decimal(0))

    @property
    def landed_total(self) -> Decimal:
        return sum((p.landed for p in self.points), Decimal("0.00"))

    @property
    def average(self) -> Decimal | None:
        pts = self.counted
        qty = sum((p.quantity for p in pts), Decimal(0))
        if not qty:
            return None
        return (sum((p.landed for p in pts), Decimal(0)) / qty).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    @property
    def last(self) -> Point | None:
        pts = self.counted
        return pts[-1] if pts else None

    @property
    def previous(self) -> Point | None:
        pts = self.counted
        return pts[-2] if len(pts) > 1 else None

    @property
    def change(self) -> Decimal | None:
        """Last landed cost per unit against the shipment before it, in percent."""
        last, prev = self.last, self.previous
        if not last or not prev or not prev.per_unit:
            return None
        return ((last.per_unit - prev.per_unit) / prev.per_unit * 100).quantize(Decimal("0.1"),
                                                                                  rounding=ROUND_HALF_UP)

    @property
    def uplift(self) -> Decimal | None:
        goods = sum((p.goods for p in self.points), Decimal(0))
        if not goods:
            return None
        charges = sum((p.charges for p in self.points), Decimal(0))
        return (charges / goods * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    @property
    def spark(self) -> str:
        """SVG polyline points (viewBox 0 0 100 28) of landed cost per unit, oldest to newest, last 12."""
        values = [p.per_unit for p in self.counted][-12:]
        if len(values) < 2:
            return ""
        lo, hi = min(values), max(values)
        out = []
        for i, v in enumerate(values):
            x = Decimal(i * 100) / (len(values) - 1)
            y = Decimal(14) if hi == lo else Decimal(25) - (v - lo) / (hi - lo) * 22
            out.append(f"{x:.1f},{y:.1f}")
        return " ".join(out)


@dataclass
class Report:
    start: date
    end: date
    currency: str
    rows: list[ProductRow] = field(default_factory=list)
    shipments: int = 0
    in_review: int = 0
    incomplete: list[str] = field(default_factory=list)      # approved shipments without a landed cost
    not_frozen_yet: int = 0                                   # left for the next page view
    other_currency: int = 0

    @property
    def landed_total(self) -> Decimal:
        return sum((r.landed_total for r in self.rows), Decimal("0.00"))

    @property
    def uplift(self) -> Decimal | None:
        goods = sum((p.goods for r in self.rows for p in r.points), Decimal(0))
        if not goods:
            return None
        charges = sum((p.charges for r in self.rows for p in r.points), Decimal(0))
        return (charges / goods * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def build(org, start: date, end: date, include_open: bool = False, q: str = "") -> Report:
    report = Report(start=start, end=end, currency=org.home_currency)
    missing = list(Shipment.objects.filter(organization=org, status__in=[Shipment.Status.APPROVED,
                                                                          Shipment.Status.POSTED],
                                           landed_run__isnull=True).select_related("organization")
                   .order_by("-approved_at")[:FREEZE_PER_VIEW + 1])
    for s in missing[:FREEZE_PER_VIEW]:
        landed.freeze(s)
    report.not_frozen_yet = max(0, len(missing) - FREEZE_PER_VIEW)
    runs = LandedCostRun.objects.filter(organization=org, as_of__range=(start, end)).select_related("shipment")
    report.incomplete = [r.shipment.reference for r in runs if not r.complete]
    report.other_currency = runs.filter(complete=True).exclude(currency=org.home_currency).count()
    rows: dict[str, ProductRow] = {}
    lines = (LandedCostLine.objects.filter(organization=org, as_of__range=(start, end), run__complete=True,
                                           run__currency=org.home_currency).select_related("shipment"))
    shipments = set()
    for line in lines:
        row = rows.setdefault(line.product_key, ProductRow(line.product_key, line.sku, line.description,
                                                           line.vendor_name, line.hs_code))
        row.points.append(Point(line.as_of, line.shipment_id, line.shipment.reference, line.quantity,
                                line.goods_value, line.charges, line.landed_total, line.per_unit))
        shipments.add(line.shipment_id)
    if include_open:
        live = (Shipment.objects.filter(organization=org, status__in=[Shipment.Status.OPEN, Shipment.Status.NEEDS_REVIEW,
                                                                        Shipment.Status.READY])
                .select_related("organization").order_by("-updated_at")[:LIVE_LIMIT])
        for s in live:
            lc = landed.compute(s)
            if not lc.complete or not (start <= lc.as_of <= end):
                continue
            report.in_review += 1
            for p in lc.products:
                row = rows.setdefault(p.product_key, ProductRow(p.product_key, p.sku, p.description, p.vendor_name,
                                                                p.hs_code))
                row.points.append(Point(lc.as_of, s.pk, s.reference, p.quantity, p.value_home or Decimal("0.00"),
                                        p.charges, p.landed or Decimal("0.00"), p.per_unit, in_review=True))
            shipments.add(s.pk)
    report.shipments = len(shipments)
    q = (q or "").strip().lower()
    out = [r for r in rows.values()
           if not q or q in f"{r.sku} {r.description} {r.vendor_name} {r.hs_code}".lower()]
    for r in out:
        r.points.sort(key=lambda p: (p.as_of, p.shipment_id))
    report.rows = sorted(out, key=lambda r: (-r.landed_total, r.description.lower()))
    return report


def today() -> date:
    return timezone.localdate()
