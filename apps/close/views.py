"""Month-end: accruals for a period, locked versions, exports; vendor statements and payments.

Permissions (decided for this feature and documented in the README):
  * audit: see Month-end, accrual reports, locked versions, statements and their reconciliation, and download
    every export. Approvers and admins have it; month-end figures are finance controls, like the audit log.
  * approve: lock a period (create a version), adjust a shipment's accrual (enter an amount or mark a charge as
    not needed), upload, correct, match again and delete statements, mark findings resolved, record payments
    and read payments from QuickBooks.
  * manage: month-end settings (accounts, which charges every shipment should carry, look-back window).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounting.models import QBOConnection, vendor_key
from apps.core.money import AmountError, parse_amount
from apps.core.paging import Paginator
from apps.core.permissions import has_perm, require
from apps.core.utils import audit, current_org
from apps.documents.services.ingest import RejectedFile
from apps.shipments.models import Shipment

from . import groups
from .forms import CloseSettingsForm
from .models import AccrualAdjustment, AccrualSnapshot, CloseSettings, ReconItem, VendorPayment, VendorStatement
from .services import accruals, exports, reconcile
from .services import statements as statement_service
from .services.statement_reader import StatementUnreadable

BUCKET_ORDER = [ReconItem.Bucket.AMOUNT_DIFFERS, ReconItem.Bucket.MISSING, ReconItem.Bucket.NOT_ON_STATEMENT,
                ReconItem.Bucket.CREDIT_NOT_APPLIED, ReconItem.Bucket.DUPLICATE, ReconItem.Bucket.PAYMENT_NOT_APPLIED,
                ReconItem.Bucket.PAYMENT_UNKNOWN, ReconItem.Bucket.OPENING, ReconItem.Bucket.ARITHMETIC]
BUCKET_HELP = {
    ReconItem.Bucket.AMOUNT_DIFFERS: "Same invoice, different amount.",
    ReconItem.Bucket.MISSING: "The vendor bills these, but ShipMatch never received them. Ask for a copy.",
    ReconItem.Bucket.NOT_ON_STATEMENT: "ShipMatch has these, the vendor doesn't list them.",
    ReconItem.Bucket.CREDIT_NOT_APPLIED: "Credits you have that the vendor's balance doesn't include.",
    ReconItem.Bucket.DUPLICATE: "Listed more than once on the statement.",
    ReconItem.Bucket.PAYMENT_NOT_APPLIED: "Payments you made that the statement doesn't show.",
    ReconItem.Bucket.PAYMENT_UNKNOWN: "Payments on the statement that aren't recorded in ShipMatch.",
    ReconItem.Bucket.OPENING: "The balance the statement starts from.",
    ReconItem.Bucket.ARITHMETIC: "The statement's own total.",
    ReconItem.Bucket.MATCHED: "Same number and amount in ShipMatch.",
    ReconItem.Bucket.SETTLED: "Paid in full, so an open-items statement no longer lists them.",
}


def _period(raw: str | None, request=None) -> date:
    """The period asked for, or the last month-end. With `request`, a value that isn't a date says so instead
    of silently showing a different month."""
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            if request is not None:
                messages.warning(request, f"“{raw[:30]}” isn't a date, so the latest month-end is shown.")
    return accruals.previous_month_end()


def _money(value) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal("0.00")


# ---------------------------------------------------------------- accruals


@login_required
def index(request):
    org = current_org(request)
    require(request.user, org, "audit")
    return redirect("close:accruals")


@login_required
def accrual_report(request):
    org = current_org(request)
    require(request.user, org, "audit")
    period = _period(request.GET.get("period"), request)
    versions = list(AccrualSnapshot.objects.filter(organization=org, period_end=period)
                    .select_related("locked_by").order_by("-version"))
    snapshot, live = None, request.GET.get("live") == "1" or not versions
    if not live:
        wanted = request.GET.get("version")
        snapshot = next((v for v in versions if str(v.version) == wanted), versions[0])
        report = snapshot.report
    else:
        report = accruals.build(org, period)
    changes = accruals.compare(versions[0].report, report) if live and versions else None
    show = request.GET.get("show", "all")
    lines = report["lines"]
    if show == "received":
        lines = [ln for ln in lines if ln["kind"] == accruals.RECEIVED]
    elif show == "estimate":
        lines = [ln for ln in lines if ln["kind"] == accruals.ESTIMATE]
    elif show == "attention":
        lines = [ln for ln in lines if ln["amount_home"] is None or (ln["confidence"] is not None
                                                                     and ln["confidence"] < 0.55)]
    else:
        show = "all"
    journal_rows, journal_total = exports.journal_lines(report)
    cfg = CloseSettings.for_org(org)
    month_ends = accruals.recent_month_ends(12)
    attention = sum(1 for ln in report["lines"] if ln["amount_home"] is None or (
        ln["confidence"] is not None and ln["confidence"] < 0.55))
    used = report.get("settings") or {}
    expected_text = "; ".join(
        f"{groups.label(g).lower()} ({dict(CloseSettings.Expect.choices).get(used.get(f'expect_{g}'), '').lower()})"
        for g in groups.EXPECTABLE if used.get(f"expect_{g}", "never") != "never") or "nothing"
    return render(request, "close/accruals.html", {
        "period": period, "report": report, "expected_text": expected_text,
        "reverse_on": period + timedelta(days=1), "lines": lines, "show": show, "versions": versions,
        "snapshot": snapshot, "live": live, "changes": changes, "journal_rows": journal_rows,
        "journal_total": journal_total, "cfg": cfg, "month_ends": month_ends,
        "custom_period": period not in month_ends, "attention": attention,
        "can_act": has_perm(request.user, org, "approve"),
        "period_ended": period < timezone.localdate(),   # a period can be locked only after its last day
        "next_version": (versions[0].version + 1) if versions else 1,
        "query": f"period={period.isoformat()}" + (f"&version={snapshot.version}" if snapshot else "&live=1"),
    })


def _report_for_export(request, org) -> tuple[dict, AccrualSnapshot | None, date]:
    period = _period(request.GET.get("period"))
    snapshot = None
    if request.GET.get("live") != "1":
        qs = AccrualSnapshot.objects.filter(organization=org, period_end=period).select_related("locked_by")
        wanted = request.GET.get("version")
        snapshot = (qs.filter(version=wanted).first() if wanted and wanted.isdigit() else qs.order_by("-version").first())
        if wanted and snapshot is None:
            raise Http404("That version doesn't exist.")
    report = snapshot.report if snapshot else accruals.build(org, period)
    return report, snapshot, period


@login_required
def accruals_csv(request):
    org = current_org(request)
    require(request.user, org, "audit")
    report, snapshot, period = _report_for_export(request, org)
    tag = f"v{snapshot.version}" if snapshot else "live"
    audit(org, "close.accruals_exported", snapshot or org, actor=request.user, period=period.isoformat(),
          format="CSV", version=snapshot.version if snapshot else "live")
    resp = HttpResponse(exports.csv_bytes(report, snapshot.version if snapshot else None),
                        content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="accruals-{period.isoformat()}-{tag}.csv"'
    return resp


@login_required
def accruals_journal(request):
    org = current_org(request)
    require(request.user, org, "audit")
    report, snapshot, period = _report_for_export(request, org)
    cfg = CloseSettings.for_org(org)
    settings_used = report.get("settings") or {}
    tag = f"v{snapshot.version}" if snapshot else "live"
    locked_by = ""
    if snapshot and snapshot.locked_by:
        locked_by = snapshot.locked_by.get_full_name() or snapshot.locked_by.get_username()
    content = exports.journal_xlsx(
        report, org.name, settings_used.get("accrued_account") or cfg.accrued_account,
        settings_used.get("accrued_account_id", cfg.accrued_account_id),
        version=snapshot.version if snapshot else None, locked_by=locked_by,
        locked_at=timezone.localtime(snapshot.locked_at).strftime("%Y-%m-%d %H:%M %Z") if snapshot else "",
        checksum=snapshot.checksum if snapshot else "")
    audit(org, "close.accruals_exported", snapshot or org, actor=request.user, period=period.isoformat(),
          format="journal entry (Excel)", version=snapshot.version if snapshot else "live")
    resp = HttpResponse(content, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="accrual-journal-{period.isoformat()}-{tag}.xlsx"'
    return resp


@login_required
@require_POST
def lock_period(request):
    org = current_org(request)
    require(request.user, org, "approve")
    # A lock is permanent, so the period must be named exactly: a missing or garbled value must never fall back
    # to "the latest month-end" and lock that.
    try:
        period = date.fromisoformat(request.POST.get("period", "").strip())
    except ValueError:
        messages.error(request, "Choose which period to lock. Nothing was locked.")
        return redirect(reverse("close:accruals"))
    url = f"{reverse('close:accruals')}?period={period.isoformat()}"
    if period >= timezone.localdate():   # "period ending 4 Oct" ends when 4 Oct is over
        messages.error(request, "This period hasn't ended yet. Lock a period after its last day.")
        return redirect(url + "&live=1")
    try:
        expected = int(request.POST.get("expected_version", "0"))
    except ValueError:
        expected = None
    try:
        snap = accruals.lock(org, period, request.user, request.POST.get("note", ""), expected_version=expected)
    except accruals.LockError as e:
        messages.error(request, str(e))
        return redirect(url + "&live=1")
    messages.success(request, f"Locked version {snap.version} for {period.day} {period:%b %Y}: {snap.currency} "
                              f"{snap.total:,.2f}. Download the journal entry to book it.")
    return redirect(f"{url}&version={snap.version}")


@login_required
@require_POST
def adjust(request):
    org = current_org(request)
    require(request.user, org, "approve")
    shipment = get_object_or_404(Shipment, organization=org, pk=request.POST.get("shipment"))
    group = request.POST.get("group", "")
    period = _period(request.POST.get("period"))
    url = f"{reverse('close:accruals')}?period={period.isoformat()}&live=1"
    if group not in groups.EXPECTABLE:
        messages.error(request, "Choose which charges this is about.")
        return redirect(url)
    action = request.POST.get("action")
    note = (request.POST.get("note") or "").strip()
    if not note:
        messages.error(request, "Add a note saying why, so whoever reviews the accrual can follow it.")
        return redirect(url)
    amount, vendor = None, (request.POST.get("vendor") or "").strip()[:200]
    if action == AccrualAdjustment.Action.AMOUNT:
        try:
            amount = parse_amount(request.POST.get("amount"))
        except AmountError as e:
            amount = None
            if e.kind == "range":
                messages.error(request, "That amount is too large. Check the number.")
                return redirect(url)
        if amount is None or amount <= 0:
            messages.error(request, "Type the expected amount as a number above zero, for example 1850.00.")
            return redirect(url)
    elif action != AccrualAdjustment.Action.EXCLUDE:
        messages.error(request, "Choose whether to enter an amount or mark the charges as not needed.")
        return redirect(url)
    adj, _ = AccrualAdjustment.objects.update_or_create(
        organization=org, shipment=shipment, group=group,
        defaults={"action": action, "amount": amount, "currency": org.home_currency, "vendor_name": vendor,
                  "note": note[:500], "created_by": request.user})
    what = (f"{org.home_currency} {amount:,.2f}" + (f" from {vendor}" if vendor else "")) if amount else "not needed"
    audit(org, "close.accrual_adjusted", adj, actor=request.user, shipment=shipment.reference,
          group=groups.label(group).lower(), what=what, note=note[:300])
    messages.success(request, f"{groups.label(group)} for {shipment.reference}: {what}.")
    return redirect(url)


@login_required
@require_POST
def remove_adjustment(request, pk: int):
    org = current_org(request)
    require(request.user, org, "approve")
    adj = get_object_or_404(AccrualAdjustment.objects.select_related("shipment"), organization=org, pk=pk)
    period = _period(request.POST.get("period"))
    audit(org, "close.accrual_adjustment_removed", adj, actor=request.user, shipment=adj.shipment.reference,
          group=groups.label(adj.group).lower())
    adj.delete()
    messages.success(request, "Adjustment removed. The charges are estimated again.")
    return redirect(f"{reverse('close:accruals')}?period={period.isoformat()}&live=1")


# ---------------------------------------------------------------- settings


@login_required
def close_settings(request):
    org = current_org(request)
    require(request.user, org, "manage")
    cfg = CloseSettings.for_org(org)
    before = cfg.as_dict() if cfg.pk else {}
    form = CloseSettingsForm(request.POST or None, instance=cfg)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.organization, obj.updated_by = org, request.user
        obj.save()
        after = obj.as_dict()
        audit(org, "close.settings_updated", obj, actor=request.user,
              changes={k: [before.get(k), v] for k, v in after.items() if str(before.get(k)) != str(v)})
        messages.success(request, "Month-end settings saved. They apply the next time a report runs; locked "
                                  "versions keep the settings they were made with.")
        return redirect("close:settings")
    return render(request, "close/settings.html", {"form": form, "cfg": cfg})


# ---------------------------------------------------------------- statements


@login_required
def statement_list(request):
    org = current_org(request)
    require(request.user, org, "audit")
    qs = VendorStatement.objects.filter(organization=org).select_related("uploaded_by")
    vendor = request.GET.get("vendor", "")
    if vendor:
        qs = qs.filter(vendor_key=vendor)
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    payments = VendorPayment.objects.filter(organization=org).select_related("created_by")[:15]
    return render(request, "close/statements.html", {
        "page": page, "statements": page.object_list, "payments": payments, "vendor": vendor,
        "vendors": reconcile.known_vendors(org), "can_act": has_perm(request.user, org, "approve"),
        "qbo": QBOConnection.objects.filter(organization=org).first(), "today": timezone.localdate(),
        "query": urlencode({"vendor": vendor}) if vendor else "",
    })


@login_required
@require_POST
def statement_upload(request):
    org = current_org(request)
    require(request.user, org, "approve")
    f = request.FILES.get("file")
    if not f:
        messages.error(request, "Choose the vendor's statement file (PDF, Excel or CSV) first.")
        return redirect("close:statements")
    limit = settings.INTAKE_MAX_FILE_MB * 1024 * 1024
    if f.size > limit:
        messages.error(request, f"{f.name}: larger than {settings.INTAKE_MAX_FILE_MB} MB. Ask the vendor for the statement "
                                "as a smaller PDF or an Excel file.")
        return redirect("close:statements")
    try:
        content = f.read()
        st, created = statement_service.upload(org, f.name, content, request.user)
    except (RejectedFile, StatementUnreadable) as e:
        messages.error(request, str(e))
        return redirect("close:statements")
    if not created:
        messages.info(request, f"This file was uploaded before ({st.original_filename}); showing that statement.")
    elif st.status == VendorStatement.Status.NEEDS_VENDOR:
        messages.warning(request, "Statement read. Choose the vendor it is from to match it.")
    else:
        messages.success(request, f"Statement from {st.vendor_name} read: {st.lines.count()} lines matched against "
                                  "ShipMatch.")
    return redirect("close:statement", pk=st.pk)


def _statement(request, pk: int, perm: str = "audit") -> VendorStatement:
    org = current_org(request)
    require(request.user, org, perm)
    return get_object_or_404(VendorStatement.objects.select_related("uploaded_by"), organization=org, pk=pk)


@login_required
def statement_detail(request, pk: int):
    st = _statement(request, pk)
    org = st.organization
    items = list(st.items.select_related("line", "document", "payment", "resolved_by"))
    by_bucket: dict[str, list] = {}
    for item in items:
        by_bucket.setdefault(item.bucket, []).append(item)
    sections = []
    for bucket in BUCKET_ORDER + [ReconItem.Bucket.MATCHED, ReconItem.Bucket.SETTLED]:
        rows = by_bucket.get(bucket) or []
        if not rows:
            continue
        if bucket == ReconItem.Bucket.MISSING:
            for item in rows:
                item.mailto = reconcile.copy_request_mailto(org, st, item)
        sections.append({
            "bucket": bucket, "label": ReconItem.Bucket(bucket).label, "help": BUCKET_HELP.get(bucket, ""),
            "items": rows, "effect": sum((i.effect for i in rows), Decimal("0.00")),
            "open": sum(1 for i in rows if not i.resolved), "quiet": bucket in ReconItem.QUIET,
        })
    payments = VendorPayment.objects.filter(organization=org, vendor_key=st.vendor_key).select_related("created_by")[:20] \
        if st.vendor_key else []
    return render(request, "close/statement.html", {
        "st": st, "summary": st.summary or {}, "sections": sections, "lines": st.lines.all(),
        "payments": payments, "vendors": reconcile.known_vendors(org),
        "can_act": has_perm(request.user, org, "approve"), "today": timezone.localdate(),
        "open_count": sum(s["open"] for s in sections if not s["quiet"]),
    })


@login_required
def statement_file(request, pk: int):
    st = _statement(request, pk)
    content_type = {"pdf": "application/pdf", "csv": "text/csv",
                    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}.get(
        st.source_format, "application/octet-stream")
    return FileResponse(st.file.open("rb"), as_attachment=True, filename=st.original_filename,
                        content_type=content_type)


@login_required
def statement_export(request, pk: int):
    import csv
    import io

    from apps.core import csvsafe

    st = _statement(request, pk)
    out = io.StringIO()
    w = csv.writer(out)
    cur = (st.summary or {}).get("currency") or st.currency
    w.writerow(["Vendor", "Statement date", "Finding", "Item", "Line", "Date", "Number", "Reference",
                "Statement amount", "ShipMatch amount", "Effect on difference", "Currency", "Explanation", "Resolved",
                "Resolved by", "Note"])
    for i in st.items.select_related("line", "resolved_by").order_by("bucket", "id"):
        w.writerow(csvsafe.row([
            st.vendor_name, st.statement_date or "", i.get_bucket_display(), i.label,
            i.line.position if i.line else "", (i.line.date if i.line and i.line.date else ""),
            i.line.number if i.line else "", i.line.reference if i.line else "",
            i.statement_amount if i.statement_amount is not None else "",
            i.shipmatch_amount if i.shipmatch_amount is not None else "", i.effect, cur, i.explanation,
            "yes" if i.resolved else "no",
            (i.resolved_by.get_full_name() or i.resolved_by.get_username()) if i.resolved_by else "",
            i.resolution_note]))
    s = st.summary or {}
    w.writerow([])
    for label, key in (("Statement balance", "statement_balance"), ("ShipMatch balance", "shipmatch_balance"),
                       ("Difference", "difference"), ("Explained", "explained"), ("Unexplained", "unexplained")):
        w.writerow(csvsafe.row([label, s.get(key, ""), cur]))
    audit(st.organization, "close.statement_exported", st, actor=request.user, vendor=st.vendor_name)
    resp = HttpResponse(out.getvalue().encode("utf-8-sig"), content_type="text/csv; charset=utf-8")
    name = vendor_key(st.vendor_name).replace(" ", "-") or "vendor"
    resp["Content-Disposition"] = f'attachment; filename="statement-{name}-{st.statement_date or st.pk}.csv"'
    return resp


@login_required
@require_POST
def statement_edit(request, pk: int):
    st = _statement(request, pk, "approve")
    org = st.organization
    before = {"vendor": st.vendor_name, "date": str(st.statement_date or ""), "currency": st.currency,
              "closing": str(st.closing_balance if st.closing_balance is not None else "")}
    name = (request.POST.get("vendor") or "").strip()
    if not vendor_key(name):
        messages.error(request, "Choose the vendor this statement is from.")
        return redirect("close:statement", pk=st.pk)
    raw_date = (request.POST.get("statement_date") or "").strip()
    try:
        day = date.fromisoformat(raw_date) if raw_date else None
    except ValueError:
        messages.error(request, "Type the statement date as a date, for example 2026-09-30.")
        return redirect("close:statement", pk=st.pk)
    cur = (request.POST.get("currency") or "").strip().upper()
    if cur and (len(cur) != 3 or not cur.isalpha()):
        messages.error(request, "Type the currency as a three-letter code, for example USD.")
        return redirect("close:statement", pk=st.pk)
    raw_closing = (request.POST.get("closing_balance") or "").strip()
    try:
        closing = parse_amount(raw_closing, allow_negative=True) if raw_closing else None
    except AmountError as e:
        messages.error(request, "That balance is too large. Check the number." if e.kind == "range"
                       else "Type the closing balance as a number, for example 12500.00, or leave it empty.")
        return redirect("close:statement", pk=st.pk)
    st.set_vendor(name)
    st.statement_date, st.currency, st.closing_balance = day, cur or org.home_currency, closing
    st.status = VendorStatement.Status.READY
    st.save()
    after = {"vendor": st.vendor_name, "date": str(st.statement_date or ""), "currency": st.currency,
             "closing": str(st.closing_balance if st.closing_balance is not None else "")}
    audit(org, "close.statement_updated", st, actor=request.user, vendor=st.vendor_name,
          changes={k: [before[k], v] for k, v in after.items() if before[k] != v})
    reconcile.run(st)
    messages.success(request, "Statement details saved and matched again.")
    return redirect("close:statement", pk=st.pk)


@login_required
@require_POST
def statement_rematch(request, pk: int):
    st = _statement(request, pk, "approve")
    reconcile.run(st)
    audit(st.organization, "close.statement_rematched", st, actor=request.user, vendor=st.vendor_name,
          difference=(st.summary or {}).get("difference"))
    messages.success(request, "Matched again with what ShipMatch has now.")
    return redirect("close:statement", pk=st.pk)


@login_required
@require_POST
def statement_delete(request, pk: int):
    st = _statement(request, pk, "approve")
    audit(st.organization, "close.statement_deleted", st, actor=request.user, vendor=st.vendor_name or "?",
          filename=st.original_filename, statement_date=str(st.statement_date or ""))
    st.file.delete(save=False)
    st.delete()
    messages.success(request, "Statement deleted. Its findings and notes are kept in the audit log.")
    return redirect("close:statements")


@login_required
@require_POST
def item_resolve(request, pk: int):
    org = current_org(request)
    require(request.user, org, "approve")
    item = get_object_or_404(ReconItem.objects.select_related("statement"), statement__organization=org, pk=pk)
    st = item.statement
    if request.POST.get("reopen") == "1":
        item.resolved, item.resolved_by, item.resolved_at, item.resolution_note = False, None, None, ""
        item.save(update_fields=["resolved", "resolved_by", "resolved_at", "resolution_note"])
        audit(org, "close.item_reopened", item, actor=request.user, item=item.label, vendor=st.vendor_name,
              bucket=item.get_bucket_display())
        messages.success(request, f"{item.label} is open again.")
        return redirect(f"{reverse('close:statement', args=[st.pk])}#item-{item.pk}")
    note = (request.POST.get("note") or "").strip()
    if not note:
        messages.error(request, "Add a note saying how it was resolved (for example: copy requested, credit applied "
                                "by the vendor on 5 Oct).")
        return redirect(f"{reverse('close:statement', args=[st.pk])}#item-{item.pk}")
    item.resolved, item.resolved_by, item.resolved_at = True, request.user, timezone.now()
    item.resolution_note = note[:500]
    item.save(update_fields=["resolved", "resolved_by", "resolved_at", "resolution_note"])
    audit(org, "close.item_resolved", item, actor=request.user, item=item.label, vendor=st.vendor_name,
          bucket=item.get_bucket_display(), effect=f"{item.effect:.2f}", note=note[:300])
    messages.success(request, f"{item.label} marked resolved.")
    return redirect(f"{reverse('close:statement', args=[st.pk])}#item-{item.pk}")


# ---------------------------------------------------------------- payments


def _back(request, fallback: str):
    st = request.POST.get("statement")
    if st and st.isdigit():
        return redirect("close:statement", pk=int(st))
    return redirect(fallback)


@login_required
@require_POST
def payment_add(request):
    org = current_org(request)
    require(request.user, org, "approve")
    try:
        pay, unknown = statement_service.record_payment(
            org, request.user, vendor_name=request.POST.get("vendor", ""), paid_on=request.POST.get("paid_on", ""),
            amount=request.POST.get("amount", ""), currency=request.POST.get("currency", ""),
            reference=request.POST.get("reference", ""), invoices=request.POST.get("invoices", ""),
            note=request.POST.get("note", ""))
    except statement_service.PaymentError as e:
        messages.error(request, str(e))
        return _back(request, "close:statements")
    text = f"Payment of {pay.currency} {pay.amount:,.2f} to {pay.vendor_name} recorded."
    if unknown:
        text += f" ShipMatch has no invoice {', '.join(unknown)} from this vendor; it is kept as typed."
    messages.success(request, text)
    _rematch_vendor(org, pay.vendor_key)
    return _back(request, "close:statements")


@login_required
@require_POST
def payment_delete(request, pk: int):
    org = current_org(request)
    require(request.user, org, "approve")
    pay = get_object_or_404(VendorPayment, organization=org, pk=pk)
    if pay.source == VendorPayment.Source.QUICKBOOKS:
        messages.error(request, "This payment comes from QuickBooks. Change it there; it is read again on the next "
                                "update.")
        return _back(request, "close:statements")
    audit(org, "close.payment_deleted", pay, actor=request.user, vendor=pay.vendor_name,
          amount=f"{pay.currency} {pay.amount:,.2f}", paid_on=pay.paid_on.isoformat())
    vk = pay.vendor_key
    pay.delete()
    _rematch_vendor(org, vk)
    messages.success(request, "Payment deleted.")
    return _back(request, "close:statements")


@login_required
@require_POST
def payment_sync(request):
    org = current_org(request)
    require(request.user, org, "approve")
    try:
        result = statement_service.sync_quickbooks_payments(org, request.user)
    except statement_service.SyncError as e:
        messages.error(request, str(e))
        return redirect("close:statements")
    for st in VendorStatement.objects.filter(organization=org, status=VendorStatement.Status.READY):
        reconcile.run(st)
    messages.success(request, f"Read payments from QuickBooks since {result['since']}: {result['created']} new, "
                              f"{result['updated']} updated.")
    return redirect("close:statements")


def _rematch_vendor(org, vk: str) -> None:
    for st in VendorStatement.objects.filter(organization=org, vendor_key=vk, status=VendorStatement.Status.READY):
        reconcile.run(st)
