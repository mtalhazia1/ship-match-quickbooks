"""Landed cost (shipment section, exports, per-product report, settings) and shared-invoice actions.

| Permission | Can |
| --- | --- |
| view   | See landed cost and shared invoices, export, open the report |
| edit   | Change how one shipment's charges are spread; split, confirm, reset or un-share an invoice |
| manage | Change the organization's landed cost settings |
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.core.permissions import require
from apps.core.utils import audit, current_org, orgs_for_user, use_org
from apps.documents.models import Document
from apps.documents.services.normalize import parse_date
from apps.shipments.models import Shipment

from .models import Category, LandedSettings, Method, SharedInvoice, ShipmentLandedOverride
from .services import allocation, exports, landed
from .services import report as report_service

DISMISS_NOTE_MIN = 5


def _shipment_for(request, pk, perm="view") -> Shipment:
    shipment = get_object_or_404(Shipment.objects.filter(organization__in=orgs_for_user(request.user))
                                 .select_related("organization"), pk=pk)
    use_org(request, shipment.organization)
    require(request.user, shipment.organization, perm)
    return shipment


def _invoice_for(request, pk, perm="edit") -> Document:
    doc = get_object_or_404(Document.objects.filter(organization__in=orgs_for_user(request.user))
                            .select_related("organization", "match__shipment"), pk=pk)
    use_org(request, doc.organization)
    require(request.user, doc.organization, perm)
    return doc


def _back(request, doc: Document | None = None, shipment: Shipment | None = None, anchor="shared-invoices") -> str:
    nxt = request.POST.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=False) \
            and nxt.startswith("/"):
        return nxt
    target = shipment or (doc.match.shipment if doc is not None and hasattr(doc, "match") else None)
    if target is None:
        return reverse("review:queue")
    return f"{reverse('review:shipment', args=[target.pk])}#{anchor}"


def _policy_from_post(post) -> tuple[str, dict]:
    method = post.get("method", "")
    if method not in Method.values:
        raise ValueError("Choose how to spread the charges.")
    by_category = {}
    for cat in Category.values:
        value = post.get(f"cat_{cat}", "")
        if value:
            if value not in Method.values:
                raise ValueError("Choose a method for each type of charge, or leave it on the default.")
            if value != method:
                by_category[cat] = value
    return method, by_category


def _describe(method: str, by_category: dict) -> str:
    return landed.Policy(method, by_category).describe()


# ---------------------------------------------------------------- shipment section


@login_required
@require_POST
def shipment_method(request, pk):
    shipment = _shipment_for(request, pk, "edit")
    back = f"{reverse('review:shipment', args=[pk])}#landed-cost"
    if shipment.is_locked:
        messages.error(request, f"{shipment.reference} is {shipment.get_status_display().lower()}, so its landed cost "
                                "is frozen. Reopen it to change how charges are spread.")
        return redirect(back)
    if request.POST.get("action") == "clear":
        deleted, _ = ShipmentLandedOverride.objects.filter(shipment=shipment).delete()
        if deleted:
            org_policy = LandedSettings.for_org(shipment.organization)
            audit(shipment.organization, "landed.method_changed", shipment, actor=request.user,
                  policy="the organization's setting, " + _describe(org_policy.method, org_policy.by_category))
            messages.success(request, "This shipment uses the organization's landed cost setting again.")
        return redirect(back)
    try:
        method, by_category = _policy_from_post(request.POST)
    except ValueError as e:
        messages.error(request, str(e))
        return redirect(back)
    ShipmentLandedOverride.objects.update_or_create(shipment=shipment, defaults={
        "method": method, "by_category": by_category, "updated_by": request.user})
    audit(shipment.organization, "landed.method_changed", shipment, actor=request.user,
          policy=_describe(method, by_category), method=method, by_category=by_category)
    messages.success(request, f"Charges on {shipment.reference} are now spread {_describe(method, by_category)}.")
    return redirect(back)


@login_required
def shipment_export(request, pk, fmt):
    if fmt not in ("csv", "xlsx"):
        raise Http404("Exports are CSV or Excel.")
    shipment = _shipment_for(request, pk, "view")
    lc = landed.landed_for(shipment)
    name = f"landed-cost-{shipment.reference}"
    audit(shipment.organization, "landed.exported", shipment, actor=request.user, what=f"{shipment.reference}, {fmt}")
    if fmt == "xlsx":
        response = HttpResponse(exports.shipment_xlsx(lc), content_type=exports.XLSX_TYPE)
        response["Content-Disposition"] = f'attachment; filename="{name}.xlsx"'
    else:
        response = HttpResponse(exports.shipment_csv(lc), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    return response


# ---------------------------------------------------------------- report


def _report_args(request, notify: bool = False):
    key = request.GET.get("period", "12m")
    key = key if key in dict(report_service.PERIODS) else "12m"
    start = parse_date(request.GET.get("from")) if request.GET.get("from") else None
    end = parse_date(request.GET.get("to")) if request.GET.get("to") else None
    if key == "custom" and not (start and end):
        if notify and (request.GET.get("from") or request.GET.get("to")):
            messages.warning(request, "Those dates could not be read, so the last 12 months are shown.")
        key = "12m"
    elif key == "custom" and start > end and notify:
        messages.info(request, "The start date was after the end date, so the two were swapped.")
    start, end = report_service.period_range(key, report_service.today(), start, end)
    return key, start, end, request.GET.get("open") == "1", request.GET.get("q", "")[:100]


@login_required
def report(request):
    org = current_org(request)
    require(request.user, org, "view")
    key, start, end, include_open, q = _report_args(request, notify=True)
    rep = report_service.build(org, start, end, include_open=include_open, q=q)
    return render(request, "landed/report.html", {
        "r": rep, "period": key, "periods": report_service.PERIODS, "include_open": include_open, "q": q,
        "query": request.GET.urlencode(),
    })


@login_required
def report_export(request, fmt):
    if fmt not in ("csv", "xlsx"):
        raise Http404("Exports are CSV or Excel.")
    org = current_org(request)
    require(request.user, org, "view")
    key, start, end, include_open, q = _report_args(request)
    rep = report_service.build(org, start, end, include_open=include_open, q=q)
    name = f"landed-cost-by-product-{start:%Y%m%d}-{end:%Y%m%d}"
    audit(org, "landed.exported", org, actor=request.user, what=f"by product {start} to {end}, {fmt}")
    if fmt == "xlsx":
        response = HttpResponse(exports.report_xlsx(rep), content_type=exports.XLSX_TYPE)
        response["Content-Disposition"] = f'attachment; filename="{name}.xlsx"'
    else:
        response = HttpResponse(exports.report_csv(rep), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    return response


# ---------------------------------------------------------------- settings


@login_required
def settings_view(request):
    org = current_org(request)
    require(request.user, org, "manage")
    current = LandedSettings.for_org(org)
    if request.method == "POST":
        try:
            method, by_category = _policy_from_post(request.POST)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("landed:settings")
        before = _describe(current.method, current.by_category)
        current.method, current.by_category, current.updated_by = method, by_category, request.user
        current.save()
        audit(org, "landed.settings_updated", current, actor=request.user, policy=_describe(method, by_category),
              before=before, method=method, by_category=by_category)
        messages.success(request, f"Saved. Charges are spread {_describe(method, by_category)}. Shipments already "
                                  "approved keep the landed cost frozen at approval.")
        return redirect("landed:settings")
    return render(request, "landed/settings.html", {
        "current": current, "methods": Method.choices, "categories": Category.choices,
        "policy": landed.Policy(current.method, landed.clean_by_category(current.by_category)),
    })


# ---------------------------------------------------------------- shared invoices


def _split_error(request, e) -> None:
    messages.error(request, f"Split not saved. {e}")


@login_required
@require_POST
def split_save(request, pk):
    doc = _invoice_for(request, pk)
    basis = request.POST.get("basis", "")
    amounts = {}
    for key, value in request.POST.items():
        if key.startswith("amount_") and key[7:].isdigit():
            amounts[int(key[7:])] = value
    add = [v for v in request.POST.getlist("add") if v.isdigit()]
    remove = [v for v in request.POST.getlist("remove") if v.isdigit()]
    try:
        allocation.save_split(doc, request.user, basis, amounts=amounts, add=add, remove=remove)
    except allocation.SplitError as e:
        _split_error(request, e)
        return redirect(_back(request, doc))
    messages.success(request, f"Split of invoice {allocation.invoice_number(doc)} saved and confirmed.")
    return redirect(_back(request, doc))


@login_required
@require_POST
def split_start(request, pk):
    """Split an invoice of this shipment with other shipments (one the detection didn't catch)."""
    shipment = _shipment_for(request, pk, "edit")
    doc = Document.objects.filter(pk=request.POST.get("invoice") or 0, match__shipment=shipment,
                                  doc_type=Document.DocType.FREIGHT_INVOICE).select_related("match__shipment").first()
    if doc is None:
        messages.error(request, "Choose one of this shipment's freight invoices to split.")
        return redirect(_back(request, shipment=shipment))
    add = [v for v in request.POST.getlist("add") if v.isdigit()]
    if not add:
        messages.error(request, "Choose at least one other shipment the invoice covers.")
        return redirect(_back(request, shipment=shipment))
    basis = request.POST.get("basis", SharedInvoice.Basis.CONTAINERS)
    if basis == SharedInvoice.Basis.MANUAL:
        basis = SharedInvoice.Basis.EQUAL  # amounts are typed once the shipments are in the split
    try:
        allocation.save_split(doc, request.user, basis, add=add)
    except allocation.SplitError as e:
        _split_error(request, e)
        return redirect(_back(request, shipment=shipment))
    messages.success(request, f"Invoice {allocation.invoice_number(doc)} is now split. Check the shares below; "
                              "you can type exact amounts with 'Change the split'.")
    return redirect(_back(request, shipment=shipment))


@login_required
@require_POST
def split_confirm(request, pk):
    doc = _invoice_for(request, pk)
    try:
        allocation.confirm(doc, request.user)
    except allocation.SplitError as e:
        messages.error(request, f"Not confirmed. {e}")
        return redirect(_back(request, doc))
    refs = sorted({r.shipment.reference for r in allocation.allocations(doc)})
    messages.success(request, f"Split of invoice {allocation.invoice_number(doc)} confirmed."
                              + (f" This closed the split warning on {', '.join(refs)} under your name; "
                                 "with maker-checker on, someone else must approve those shipments." if len(refs) > 1 else ""))
    return redirect(_back(request, doc))


@login_required
@require_POST
def split_dismiss(request, pk):
    doc = _invoice_for(request, pk)
    note = request.POST.get("note", "").strip()[:500]
    if len(note) < DISMISS_NOTE_MIN:
        messages.error(request, "Say why the invoice isn't shared, so the team knows (for example: the other B/L is "
                                "only mentioned for reference).")
        return redirect(_back(request, doc))
    try:
        allocation.dismiss(doc, request.user, note)
    except allocation.SplitError as e:
        _split_error(request, e)
        return redirect(_back(request, doc))
    messages.success(request, f"Invoice {allocation.invoice_number(doc)} stays whole on "
                              f"{doc.match.shipment.reference if hasattr(doc, 'match') else 'its shipment'}.")
    return redirect(_back(request, doc))


@login_required
@require_POST
def split_reset(request, pk):
    doc = _invoice_for(request, pk)
    try:
        allocation.reset(doc, request.user)
    except allocation.SplitError as e:
        _split_error(request, e)
        return redirect(_back(request, doc))
    shared = allocation.active(doc) is not None
    messages.success(request, f"Invoice {allocation.invoice_number(doc)} "
                              + ("is split automatically again. Check the shares and confirm them." if shared
                                 else "doesn't name another open shipment, so it stays whole."))
    return redirect(_back(request, doc))
