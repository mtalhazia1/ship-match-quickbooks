"""Template helpers for the customs sections of the shipment page and the dashboard."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django import template

from apps.core.permissions import has_perm
from apps.documents.models import Document
from apps.documents.services.normalize import parse_date

from ..fees import MpfRate, expected_hmf, expected_mpf, hmf_percent
from ..models import ContainerFreeTime, CustomsSettings
from ..services import entry as E
from ..services.checks import invoice_value
from ..services.freetime import Status, org_today, status_for

register = template.Library()


@dataclass
class Figure:
    label: str
    amount: Decimal | None
    note: str = ""
    state: str = ""          # "" | ok | warn | err


@dataclass
class EntrySummary:
    doc: Document
    number: str
    date: object
    port: str
    currency: str
    figures: list[Figure] = field(default_factory=list)
    at_risk: Decimal | None = None
    lines: int = 0


@dataclass
class ShipmentCustoms:
    entries: list[EntrySummary]
    rows: list[tuple[ContainerFreeTime, Status]]
    today: object
    lead_days: int
    timezone: str

    @property
    def basis_notes(self) -> list[str]:
        """How free days were counted, once per distinct rule."""
        return list(dict.fromkeys(r.basis_note for r, _ in self.rows if r.basis_note))

    @property
    def any(self) -> bool:
        return bool(self.entries or self.rows)


def _summary(doc: Document, invoices: list[Document], issues) -> EntrySummary:
    data = doc.data()
    cur = E.entry_currency(data) or doc.organization.home_currency
    day = parse_date(data.get("entry_date")) or parse_date(data.get("import_date"))
    s = EntrySummary(doc, str(data.get("entry_number") or "No entry number"), day, str(data.get("port_of_entry") or ""),
                     cur, lines=len(data.get("entry_lines") or []))
    tol = E.duty_tolerance()
    value = E.entered_value(data)
    found = invoice_value(doc.organization, invoices, data) if invoices else None
    if found is None:
        note, state = ("No commercial invoice in this shipment" if not invoices else
                       "Invoices can't be converted (no exchange rate)"), ""
    else:
        inv, how, _ = found
        note = f"Invoices {E.money(inv, cur)}" + (f", {how}" if how else "")
        state = "ok"
        if value is not None and inv:
            pct = (value - inv) / inv * 100
            state = "ok" if abs(pct) <= E.value_tolerance_percent() else ("err" if pct < 0 else "warn")
    s.figures.append(Figure("Entered value", value, note, state))

    checks = E.line_checks(data)
    duty = E.dec(data.get("total_duty"))
    stated_lines = [c.stated for c in checks if c.stated is not None]
    line_sum = sum(stated_lines, Decimal("0.00")) if stated_lines else None
    bad_lines = sum(1 for c in checks if c.mismatch)
    if bad_lines:
        note, state = (f"{bad_lines} line{'s' if bad_lines != 1 else ''} "
                       f"{'don' if bad_lines != 1 else 'doesn'}'t match rate times value"), "err"
    elif duty is not None and line_sum is not None and abs(duty - line_sum) > tol:
        note, state = f"Lines add up to {E.money(line_sum, cur)}", "err"
    elif checks:
        unchecked = sum(1 for c in checks if not c.checkable)
        note = "Every line matches rate times value" if not unchecked else \
            f"{unchecked} line{'s' if unchecked != 1 else ''} with a specific rate not recalculated"
        state = "ok" if not unchecked else ""
    else:
        note, state = "No tariff lines read", ""
    s.figures.append(Figure("Duty", duty if duty is not None else line_sum, note, state))

    mpf = E.dec(data.get("merchandise_processing_fee"))
    if mpf is not None or E.is_us_entry(data):
        if value is not None and day is not None and E.is_us_entry(data):
            expected, r = expected_mpf(value, day)
            state = "" if mpf is None else ("ok" if abs(mpf - expected) <= tol else ("err" if mpf > expected else "warn"))
            note = _mpf_note(r, expected, cur, state == "ok")
        else:
            note, state = "Needs the entry date and entered value to check", ""
        s.figures.append(Figure("Merchandise processing fee", mpf, note, state))
    hmf = E.dec(data.get("harbor_maintenance_fee"))
    if hmf is not None:
        note, state = "", ""
        if value is not None:
            expected = expected_hmf(value)
            note = f"{hmf_percent()}% of the entered value is {E.money(expected, cur)}"
            state = "ok" if abs(hmf - expected) <= tol else ("err" if hmf > expected else "warn")
        s.figures.append(Figure("Harbor maintenance fee", hmf, note, state))
    other = E.dec(data.get("other_fees"))
    if other:
        s.figures.append(Figure("Other fees and taxes", other))
    total = E.dec(data.get("total_duty_and_fees"))
    computed = None if (duty if duty is not None else line_sum) is None else \
        (duty if duty is not None else line_sum) + E.stated_fees(data)
    if total is not None or computed is not None:
        if total is not None and computed is not None:
            note = f"Duty plus fees is {E.money(computed, cur)}"
            state = "ok" if abs(total - computed) <= tol else ("err" if total > computed else "warn")
        else:
            note, state = "", ""
        s.figures.append(Figure("Total duty and fees", total if total is not None else computed, note, state))
    risk = [i.amount_at_risk for i in issues if i.document_id == doc.pk and not i.resolved and i.amount_at_risk]
    s.at_risk = sum(risk, Decimal("0.00")) if risk else None
    return s


def _mpf_note(r: MpfRate, expected: Decimal, cur: str, correct: bool) -> str:
    since = f" from {E.day_text(r.start)}" if r.start.year > 1 else ""
    rule = (f"{r.percent}% of the entered value, between {E.money(r.minimum, cur)} and "
            f"{E.money(r.maximum, cur)}{since}")
    return rule[:1].upper() + rule[1:] if correct else f"Should be {E.money(expected, cur)}: {rule}"


@register.simple_tag
def customs_for(shipment) -> ShipmentCustoms:
    """Customs entries (with their checks in figures) and free time rows of one shipment."""
    org = shipment.organization
    free_time = list(ContainerFreeTime.objects.filter(shipment=shipment).order_by("container_number"))
    has_entry = shipment.documents.filter(doc_type=Document.DocType.CUSTOMS_ENTRY).exists()
    if not free_time and not has_entry:  # most shipments: nothing to show, nothing more to read
        return ShipmentCustoms([], [], None, 0, org.timezone or "UTC")
    entries = []
    if has_entry:
        docs = list(shipment.documents.filter(doc_type__in=[Document.DocType.CUSTOMS_ENTRY,
                                                             Document.DocType.COMMERCIAL_INVOICE])
                    .prefetch_related("fields"))
        invoices = [d for d in docs if d.doc_type == Document.DocType.COMMERCIAL_INVOICE]
        issues = list(shipment.issues.all())
        entries = [_summary(d, invoices, issues) for d in docs if d.doc_type == Document.DocType.CUSTOMS_ENTRY]
    cfg = CustomsSettings.for_org(org)
    today = org_today(org)
    rows = [(r, status_for(r, today, cfg.lfd_alert_days)) for r in free_time]
    return ShipmentCustoms(entries, rows, today, cfg.lfd_alert_days, org.timezone or "UTC")


@register.simple_tag
def entry_lines_for(doc) -> list:
    """A customs entry's lines with the duty recalculated, for the table on the document card."""
    if doc.doc_type != Document.DocType.CUSTOMS_ENTRY:
        return []
    return E.line_checks(doc.data())


@register.simple_tag
def container_dates_for(doc) -> list:
    if doc.doc_type != Document.DocType.ARRIVAL_NOTICE:
        return []
    rows = []
    for r in doc.field("container_dates") or []:
        if isinstance(r, dict):
            rows.append({k: (parse_date(v) if k != "container_number" else v) for k, v in r.items()})
    return rows


@register.inclusion_tag("customs/_dashboard_card.html", takes_context=True)
def free_time_card(context):
    """'Containers near last free day' on the dashboard, only for organizations that track free time."""
    from ..services.freetime import rows_needing_action

    request, org = context.get("request"), context.get("org")
    if request is None or org is None or not has_perm(request.user, org, "view"):
        return {"show": False}
    if not ContainerFreeTime.objects.filter(organization=org, returned_on__isnull=True).exists():
        return {"show": False}
    cfg = CustomsSettings.for_org(org)
    rows = rows_needing_action(org, limit=6)
    return {"show": True, "rows": rows, "lead_days": cfg.lfd_alert_days, "org": org,
            "open_count": ContainerFreeTime.objects.filter(organization=org, returned_on__isnull=True).count()}


@register.filter
def cu_tone(status) -> str:
    """Badge class for a free time status."""
    return {"ok": "ok", "soon": "warn", "today": "warn", "late": "err", "closed": "neutral"}.get(
        getattr(status, "tone", ""), "neutral")


@register.filter
def cu_day(value) -> str:
    return E.day_text(value) if value else "–"
