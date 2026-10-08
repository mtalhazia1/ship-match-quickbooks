"""Savings: money caught by validation and what happened to it, and the public ROI calculator.

Savings needs the view permission. Every amount on the page is already visible to a viewer on
the shipments themselves; the page only adds them up, and finance leads who follow results
without editing anything are often viewers.
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from apps.core import csvsafe
from apps.core.permissions import require
from apps.core.utils import audit, current_org
from apps.documents.services.normalize import parse_date

from . import roi
from .savings import OUTCOME_LABELS, PERIODS, period_range, summary


def _period(request, notify: bool = False) -> tuple[date, date, str, str]:
    key = request.GET.get("period", "this_month")
    key = key if key in dict(PERIODS) else "this_month"
    start = parse_date(request.GET.get("from")) if request.GET.get("from") else None
    end = parse_date(request.GET.get("to")) if request.GET.get("to") else None
    if key == "custom" and not (start and end):
        if notify and (request.GET.get("from") or request.GET.get("to")):
            messages.warning(request, "Those dates could not be read, so this month is shown.")
        key = "this_month"
    elif key == "custom" and start > end and notify:
        messages.info(request, "The start date was after the end date, so the two were swapped.")
    s, e, label = period_range(key, timezone.localdate(), start, end)
    return s, e, label, key


@login_required
def savings_summary(request):
    org = current_org(request)
    require(request.user, org, "view")
    start, end, label, key = _period(request, notify=True)
    s = summary(org, start, end)
    months, months_label = s.by_month, label
    if len(months) < 3:  # one or two bars say little: show the six months up to the period's end for context
        first = end.replace(day=1)
        for _ in range(5):
            first = (first - timedelta(days=1)).replace(day=1)
        months, months_label = summary(org, first, end, limit=0).by_month, "Last 6 months"
    return render(request, "rates/savings.html", {
        "s": s, "period_label": label, "period": key, "periods": PERIODS, "start": start, "end": end,
        "query": request.GET.urlencode(), "outcome_labels": OUTCOME_LABELS, "months": months,
        "months_label": months_label, "months_max": max((m.caught for m in months), default=0),
    })


@login_required
def savings_export(request):
    org = current_org(request)
    require(request.user, org, "view")
    start, end, label, _ = _period(request)
    s = summary(org, start, end, limit=None)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["first caught", "shipment", "document", "vendor", "issue", "charge", "currency", "amount caught",
                "latest amount", "outcome", f"prevented ({s.currency})", f"still at risk ({s.currency})",
                f"accepted ({s.currency})", f"withdrawn ({s.currency})", "note"])
    for r in s.catches:
        c = r.catch
        w.writerow(csvsafe.row([
            timezone.localtime(c.first_caught_at).strftime("%Y-%m-%d %H:%M"),
            c.shipment.reference if c.shipment else "", c.document.original_filename if c.document else "",
            c.vendor_name, r.title, c.charge_code, c.currency, f"{c.amount_caught:.2f}", f"{c.amount_latest:.2f}",
            r.outcome_label if r.amount is not None else "No exchange rate",
            *[f"{r.split.get(b, 0):.2f}" if r.amount is not None else "" for b in ("prevented", "at_risk", "accepted",
                                                                                     "withdrawn")],
            r.resolved_note,
        ]))
    audit(org, "savings.exported", org, actor=request.user, period=label, catches=len(s.catches))
    response = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (f'attachment; filename="shipmatch-savings-{org.slug}-{start:%Y%m%d}-'
                                       f'{end:%Y%m%d}.csv"')
    return response


def roi_calculator(request):
    """Public page (no sign-in): used on sales calls. Works without JavaScript; roi.js updates live."""
    calc = roi.calculate(request.GET)
    rows = []
    for f in roi.FIELDS:
        default = f"{f.default.normalize():f}"
        rows.append({"f": f, "value": calc.raw.get(f.name) or default, "default": default,
                     "error": calc.errors.get(f.name, "")})
    share_url = request.build_absolute_uri(request.path)
    if request.GET:
        share_url += "?" + request.GET.urlencode()
    template = "rates/roi.html" if request.user.is_authenticated else "rates/roi_public.html"
    return render(request, template, {
        "calc": calc, "rows": rows, "r": calc.result, "out": roi.formatted(calc), "currencies": roi.CURRENCIES,
        "share_url": share_url, "submitted": bool(request.GET)})
