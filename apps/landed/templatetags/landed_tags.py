from decimal import Decimal

from django import template

from apps.core.permissions import has_perm
from apps.documents.models import Document
from apps.shipments.models import Shipment, ValidationIssue

from ..models import Category, InvoiceAllocation, LandedSettings, Method, SharedInvoice, ShipmentLandedOverride
from ..services import allocation, landed
from ..services.charges import invoice_number

register = template.Library()
Basis = SharedInvoice.Basis
SPLIT_CHOICES = [
    (Basis.LINES, "By the lines that name each shipment"), (Basis.CONTAINERS, "By number of containers"),
    (Basis.WEIGHT, "By weight on the commercial invoices"), (Basis.VOLUME, "By volume on the commercial invoices"),
    (Basis.EQUAL, "Equally"), (Basis.MANUAL, "Amounts I type"),
]


@register.simple_tag
def shared_panel(shipment, user):
    """Shared invoices that concern this shipment, for the panel on the shipment page."""
    can_edit = has_perm(user, shipment.organization, "edit")
    items = []
    for si in allocation.for_shipment(shipment):
        doc = si.document
        if not hasattr(doc, "match"):
            continue
        primary = doc.match.shipment
        is_primary = primary.pk == shipment.pk
        data = doc.data()
        total, cur = allocation.invoice_total(doc, data)
        rows = allocation.allocations(doc)
        base = {"si": si, "doc": doc, "number": invoice_number(doc, data), "vendor": data.get("vendor_name") or "",
                "total": total, "currency": cur, "primary": primary, "is_primary": is_primary}
        if si.status == SharedInvoice.Status.DISMISSED:
            if is_primary:
                items.append({**base, "dismissed": True, "can_edit": can_edit and not primary.is_locked})
            continue
        if len(rows) < 2:
            if is_primary and (si.detected or {}).get("locked"):
                items.append({**base, "locked_only": (si.detected or {}).get("locked")})
            continue
        frozen = allocation.frozen_by(doc, rows)
        refs = (si.detected or {}).get("refs") or {}
        table = []
        for r in rows:
            pct = (r.amount / total * 100) if total else None
            table.append({"row": r, "shipment": r.shipment, "is_current": r.shipment_id == shipment.pk,
                          "is_primary": r.shipment_id == primary.pk, "refs": refs.get(str(r.shipment_id), []),
                          "pct": pct})
        mine = next((t for t in table if t["is_current"]), None)
        members = {r.shipment_id for r in rows}
        unshared = [s for s in Shipment.objects.filter(pk__in=(si.detected or {}).get("unshared") or [])
                    if s.pk not in members]
        errors = 0 if is_primary else ValidationIssue.objects.filter(
            shipment=primary, document=doc, resolved=False, severity=ValidationIssue.Severity.ERROR).count()
        items.append({**base, "rows": table, "mine": mine, "frozen": frozen, "invoice_errors": errors,
                      "confirmed": allocation.is_confirmed(si, rows), "adds_up": allocation.adds_up(doc, rows),
                      "assigned": sum((r.amount for r in rows), Decimal("0.00")),
                      "basis": si.basis, "basis_label": si.get_basis_display(), "notes": (si.detected or {}).get(
                          "notes") or [], "unshared": unshared, "others": [t for t in table if not t["is_current"]],
                      "can_edit": can_edit and not frozen,
                      "candidates": _candidates(shipment.organization, exclude=members) if can_edit and not frozen
                      else []})
    startable = []
    if can_edit and not shipment.is_locked:
        # Invoices not split right now (also ones marked "not shared", or naming only approved shipments).
        busy = set(InvoiceAllocation.objects.filter(document__match__shipment=shipment)
                   .values_list("document_id", flat=True))
        startable = [d for d in shipment.documents.filter(doc_type=Document.DocType.FREIGHT_INVOICE)
                     .prefetch_related("fields") if d.pk not in busy]
    return {"items": items, "startable": [(d, invoice_number(d)) for d in startable],
            "candidates": _candidates(shipment.organization, exclude={shipment.pk}) if startable else [],
            "choices": SPLIT_CHOICES}


def _candidates(org, exclude) -> list[Shipment]:
    return list(Shipment.objects.filter(organization=org).exclude(pk__in=list(exclude))
                .exclude(status__in=[Shipment.Status.APPROVED, Shipment.Status.POSTED]).order_by("-updated_at")[:50])


@register.simple_tag
def landed_cost(shipment):
    lc = landed.landed_for(shipment)
    override = ShipmentLandedOverride.objects.filter(shipment=shipment).first()
    org_settings = LandedSettings.for_org(shipment.organization)
    return {"lc": lc, "override": override,
            "org_policy": landed.Policy(org_settings.method, landed.clean_by_category(org_settings.by_category)),
            "methods": Method.choices, "category_choices": Category.choices,
            "current": lc.policy}


@register.filter
def pct(value):
    if value is None:
        return "–"
    return f"{Decimal(value):.1f}%"


@register.filter
def unit_money(value):
    """Per-unit cost, always 4 decimals so a column of unit costs lines up (0.4125, 58.3690, 17.9000)."""
    if value in (None, ""):
        return "–"
    return f"{Decimal(str(value)):,.4f}"


@register.filter
def qty(value):
    if value in (None, ""):
        return "–"
    v = Decimal(str(value)).normalize()
    return f"{v:,f}" if v == v.to_integral() else f"{v:,}"


@register.filter
def method_word(value):
    return landed.METHOD_WORDS.get(value, value or "")


@register.filter
def category_label(value):
    try:
        return Category(value).label
    except ValueError:
        return value


@register.filter
def lookup(mapping, key):
    try:
        return mapping.get(key, "")
    except AttributeError:
        return ""


@register.filter
def lc_category_total(lc, key):
    return lc.category_total(key)


@register.filter
def lc_div(value, by):
    try:
        return (Decimal(str(value)) / Decimal(str(by))).quantize(Decimal("0.0001"))
    except Exception:
        return None


@register.filter
def lc_abs(value):
    try:
        return abs(Decimal(str(value)))
    except Exception:
        return value
