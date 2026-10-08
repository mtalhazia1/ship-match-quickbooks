"""Numbers for the dashboard. Everything is scoped to one organization and a date window."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from statistics import median

from django.db.models import Count, Min, Q
from django.utils import timezone

from apps.accounting.models import PostedBill
from apps.documents.models import Document, ExtractedField
from apps.shipments.labels import issue_title
from apps.shipments.models import MatchLink, Shipment, ValidationIssue


@dataclass
class Bar:
    label: str
    value: float
    detail: str = ""
    x: float = 0
    y: float = 0
    w: float = 0
    h: float = 0
    path: str = ""


@dataclass
class Dashboard:
    days: int
    needs_review: int = 0
    ready: int = 0
    approved: int = 0
    posted: int = 0
    oldest_waiting_hours: float | None = None
    loose_documents: int = 0
    documents_received: int = 0
    straight_through_rate: float | None = None
    correction_rate: float | None = None
    median_cycle_hours: float | None = None
    posted_value: dict = field(default_factory=dict)
    issues_by_type: list[Bar] = field(default_factory=list)
    docs_per_day: list[Bar] = field(default_factory=list)
    docs_max: int = 0
    attention: list = field(default_factory=list)


def _round_top_bar(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """SVG path for a vertical bar with rounded top corners only (flat on the baseline)."""
    if h <= 0:
        return ""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")


def _round_end_hbar(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """SVG path for a horizontal bar with a rounded right end (flat at the axis)."""
    if w <= 0:
        return ""
    r = min(r, h / 2, w)
    return (f"M{x:.1f},{y:.1f} H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
            f"V{y + h - r:.1f} Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} H{x:.1f} Z")


def build(org, days: int = 30) -> Dashboard:
    now = timezone.now()
    since = now - timedelta(days=days)
    d = Dashboard(days=days)
    ships = Shipment.objects.filter(organization=org)
    status_counts = dict(ships.values_list("status").annotate(c=Count("id")))
    d.needs_review = status_counts.get(Shipment.Status.NEEDS_REVIEW, 0)
    d.ready = status_counts.get(Shipment.Status.READY, 0)
    d.approved = status_counts.get(Shipment.Status.APPROVED, 0)
    d.posted = status_counts.get(Shipment.Status.POSTED, 0)

    oldest = ships.filter(status=Shipment.Status.NEEDS_REVIEW).aggregate(m=Min("created_at"))["m"]
    if oldest:
        d.oldest_waiting_hours = round((now - oldest).total_seconds() / 3600, 1)
    d.loose_documents = Document.objects.filter(organization=org, status__in=[
        Document.Status.UNMATCHED, Document.Status.NEEDS_OCR, Document.Status.ERROR]).count()

    docs = Document.objects.filter(organization=org, received_at__gte=since)
    d.documents_received = docs.count()

    window_ships = ships.filter(created_at__gte=since)
    total_ships = window_ships.count()
    if total_ships:
        touched = window_ships.filter(
            Q(links__document__fields__source=ExtractedField.Source.HUMAN)
            | Q(links__method=MatchLink.Method.MANUAL)
            | Q(issues__resolved=True)
        ).distinct().count()
        d.straight_through_rate = round(100 * (total_ships - touched) / total_ships, 1)

    fields = ExtractedField.objects.filter(document__organization=org, document__received_at__gte=since)
    n_fields = fields.count()
    if n_fields:
        d.correction_rate = round(100 * fields.filter(source=ExtractedField.Source.HUMAN).count() / n_fields, 2)

    cycle = [
        (s.approved_at - s.created_at).total_seconds() / 3600
        for s in ships.filter(approved_at__gte=since).only("approved_at", "created_at")
    ]
    if cycle:
        d.median_cycle_hours = round(median(cycle), 1)

    for pb in PostedBill.objects.filter(organization=org, status=PostedBill.Status.POSTED, posted_at__gte=since
                                        ).select_related("document").prefetch_related("document__fields"):
        cur = (pb.document.field("currency") or org.home_currency).upper()
        try:
            amount = Decimal(str(pb.document.field("total_amount")))
        except Exception:
            continue
        d.posted_value[cur] = d.posted_value.get(cur, Decimal("0")) + amount

    # Open exceptions by type (horizontal bars)
    rows = (ValidationIssue.objects.filter(organization=org, resolved=False, shipment__status__in=[
        Shipment.Status.NEEDS_REVIEW, Shipment.Status.READY, Shipment.Status.OPEN])
        .values("code", "severity").annotate(n=Count("id")).order_by("-n"))
    max_n = max([r["n"] for r in rows], default=0)
    bar_h, gap, label_w, chart_w = 22, 10, 0, 300
    for i, r in enumerate(rows):
        w = chart_w * r["n"] / max_n if max_n else 0
        y = i * (bar_h + gap)
        b = Bar(label=issue_title(r["code"]), value=r["n"], detail="Error" if r["severity"] == "error" else "Warning",
                x=label_w, y=y, w=w, h=bar_h)
        b.path = _round_end_hbar(label_w, y, max(w, 2), bar_h)
        d.issues_by_type.append(b)

    # Documents received per day, last 14 days (vertical bars, local dates)
    tz = timezone.get_current_timezone()
    today = timezone.localtime(now, tz).date()
    span = 14
    counts = {today - timedelta(days=i): 0 for i in range(span)}
    for received in Document.objects.filter(organization=org, received_at__gte=now - timedelta(days=span + 1)
                                            ).values_list("received_at", flat=True):
        day = timezone.localtime(received, tz).date()
        if day in counts:
            counts[day] += 1
    max_c = max(counts.values(), default=0) or 1
    d.docs_max = max_c
    plot_h, col_w, col_gap = 120, 22, 2
    for i, day in enumerate(sorted(counts)):
        n = counts[day]
        h = plot_h * n / max_c
        x = i * (col_w + col_gap)
        b = Bar(label=day.strftime("%d %b"), value=n, x=x, y=plot_h - h, w=col_w, h=h)
        b.path = _round_top_bar(x, plot_h - h, col_w, h)
        d.docs_per_day.append(b)

    # Oldest shipments waiting for review
    d.attention = list(
        ships.filter(status=Shipment.Status.NEEDS_REVIEW)
        .annotate(n_errors=Count("issues", filter=Q(issues__resolved=False, issues__severity="error"), distinct=True),
                  n_warnings=Count("issues", filter=Q(issues__resolved=False, issues__severity="warning"), distinct=True))
        .order_by("-n_errors", "created_at")[:6]
    )
    return d
