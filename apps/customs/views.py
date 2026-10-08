"""Free time list, pickup and empty return dates, and customs settings.

Every view checks the user's role in the organization (apps.core.permissions): viewers see free time, reviewers
and up enter pickup and return dates, admins change the settings. Every change is in the audit log.
"""
from __future__ import annotations

import re
from datetime import date

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from apps.core.paging import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.core.permissions import require
from apps.core.utils import audit, current_org, orgs_for_user, use_org

from .fees import hmf_percent, mpf_rate_on, mpf_table
from .models import ContainerFreeTime, CustomsSettings
from .services.entry import day_text, duty_tolerance, value_tolerance_percent
from .services.freetime import org_today, recompute, recompute_org, status_for

TABS = [("attention", "Needs action"), ("open", "All open"), ("closed", "Returned")]
PER_PAGE = 50
MAX_LEAD_DAYS = 30


def _back(request, default: str) -> str:
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return default


def _parse_day(raw: str | None) -> date | None:
    raw = (raw or "").strip()
    return date.fromisoformat(raw) if raw else None


# --------------------------------------------------------------------------- free time list


@login_required
def free_time(request):
    org = current_org(request)
    require(request.user, org, "view")
    tab = request.GET.get("tab", "attention")
    tab = tab if tab in dict(TABS) else "attention"
    cfg = CustomsSettings.for_org(org)
    today = org_today(org)
    base = ContainerFreeTime.objects.filter(organization=org).select_related("shipment")
    open_rows = [(r, status_for(r, today, cfg.lfd_alert_days, with_cost=False)) for r in base.filter(returned_on__isnull=True)]
    counts = {"attention": sum(1 for _, s in open_rows if s.needs_action), "open": len(open_rows),
              "closed": base.filter(returned_on__isnull=False).count()}
    if tab == "closed":
        rows = [(r, status_for(r, today, cfg.lfd_alert_days, with_cost=False))
                for r in base.filter(returned_on__isnull=False).order_by("-returned_on", "container_number")]
    else:
        rows = [rs for rs in open_rows if tab == "open" or rs[1].needs_action]
        rows.sort(key=lambda rs: (rs[1].days_left is None, rs[1].days_left if rs[1].days_left is not None else 0,
                                  rs[0].container_number))
    page = Paginator(rows, PER_PAGE).get_page(request.GET.get("page"))
    page.object_list = [(r, status_for(r, today, cfg.lfd_alert_days)) for r, _ in page.object_list]
    return render(request, "customs/free_time.html", {
        "tab": tab, "tabs": [(k, v, counts[k]) for k, v in TABS], "page": page, "today": today,
        "lead_days": cfg.lfd_alert_days, "org_tz": org.timezone or "UTC"})


# --------------------------------------------------------------------------- pickup and return dates


@login_required
@require_POST
def set_dates(request, pk):
    """A person records when the container left the terminal and when the empty went back (audited)."""
    row = get_object_or_404(ContainerFreeTime.objects.filter(organization__in=orgs_for_user(request.user))
                            .select_related("organization", "shipment"), pk=pk)
    use_org(request, row.organization)
    require(request.user, row.organization, "edit")
    org = row.organization
    back = _back(request, f"{reverse('review:shipment', args=[row.shipment_id])}#free-time")
    try:
        picked, returned = _parse_day(request.POST.get("picked_up_on")), _parse_day(request.POST.get("returned_on"))
    except ValueError:
        messages.error(request, "Dates must be written like 2026-04-13. Nothing was saved.")
        return redirect(back)
    today = org_today(org)
    problem = None
    if picked and picked > today:
        problem = f"The pickup date of {row.container_number} is after today ({day_text(today)}). Enter it once the " \
                  "container has left the terminal."
    elif returned and returned > today:
        problem = f"The empty return date of {row.container_number} is after today ({day_text(today)})."
    elif returned and not picked:
        problem = f"Enter the pickup date of {row.container_number} first: an empty is returned after it is picked up."
    elif picked and returned and returned < picked:
        problem = f"The empty return date of {row.container_number} is before its pickup date."
    elif picked and row.discharge_date and not row.discharge_estimated and picked < row.discharge_date:
        problem = (f"The pickup date of {row.container_number} is before it was discharged "
                   f"({day_text(row.discharge_date)}). Check the date.")
    if problem:
        messages.error(request, problem + " Nothing was saved.")
        return redirect(back)
    changed = []
    if picked != row.picked_up_on:
        audit(org, "free_time.picked_up", row, actor=request.user, container=row.container_number,
              shipment=row.shipment.reference, old=row.picked_up_on, new=picked)
        row.picked_up_on, row.picked_up_by = picked, request.user if picked else None
        changed.append("pickup date")
    if returned != row.returned_on:
        audit(org, "free_time.returned", row, actor=request.user, container=row.container_number,
              shipment=row.shipment.reference, old=row.returned_on, new=returned)
        row.returned_on, row.returned_by = returned, request.user if returned else None
        changed.append("empty return date")
    if not changed:
        messages.info(request, f"No changes for {row.container_number}.")
        return redirect(back)
    recompute(row)
    row.save()
    done = " and ".join(changed)
    if row.returned_on:
        messages.success(request, f"Saved the {done} of {row.container_number}. Its free time is closed.")
    elif row.picked_up_on and row.lfd_detention:
        messages.success(request, f"Saved the {done} of {row.container_number}. Return the empty by "
                                  f"{day_text(row.lfd_detention)} to avoid detention.")
    else:
        messages.success(request, f"Saved the {done} of {row.container_number}.")
    return redirect(back)


# --------------------------------------------------------------------------- settings


@login_required
def customs_settings(request):
    org = current_org(request)
    require(request.user, org, "manage")
    cfg = CustomsSettings.for_org(org)
    errors = {}
    form = {"lfd_alert_days": cfg.lfd_alert_days, "count_weekends": cfg.count_weekends,
            "holidays": "\n".join(sorted(cfg.holidays or []))}
    if request.method == "POST":
        form = {"lfd_alert_days": request.POST.get("lfd_alert_days", "").strip(),
                "count_weekends": request.POST.get("count_weekends") == "on",
                "holidays": request.POST.get("holidays", "")}
        try:
            lead = int(form["lfd_alert_days"])
            if not 0 <= lead <= MAX_LEAD_DAYS:
                raise ValueError
        except ValueError:
            errors["lfd_alert_days"] = f"Enter a whole number of days from 0 to {MAX_LEAD_DAYS}."
        holidays, bad = [], []
        for token in re.split(r"[\s,;]+", form["holidays"]):
            if not token:
                continue
            try:
                holidays.append(date.fromisoformat(token).isoformat())
            except ValueError:
                bad.append(token)
        if bad:
            errors["holidays"] = (f"These aren't dates: {', '.join(bad[:5])}. Write one date per line, like "
                                  "2026-12-25.")
        if not errors:
            before = {"lfd_alert_days": cfg.lfd_alert_days, "count_weekends": cfg.count_weekends,
                      "holidays": sorted(cfg.holidays or [])}
            cfg.lfd_alert_days, cfg.count_weekends, cfg.holidays = lead, form["count_weekends"], sorted(set(holidays))
            after = {"lfd_alert_days": cfg.lfd_alert_days, "count_weekends": cfg.count_weekends,
                     "holidays": cfg.holidays}
            changes = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
            if changes:
                cfg.save()
                audit(org, "customs_settings.updated", cfg, actor=request.user, changes=changes)
                n = recompute_org(org)
                messages.success(request, "Free time settings saved." + (f" Last free days of {n} open container"
                                                                         f"{'s' if n != 1 else ''} were recalculated."
                                                                         if n else ""))
            else:
                messages.info(request, "Nothing changed.")
            return redirect("customs:settings")
        messages.error(request, "Not saved. Fix the fields marked below.")
    today = org_today(org)
    current = mpf_rate_on(today)
    return render(request, "customs/settings.html", {
        "form": form, "errors": errors, "max_lead": MAX_LEAD_DAYS, "mpf_rows": list(reversed(mpf_table()))[:6],
        "mpf_current": current, "hmf": hmf_percent(), "duty_tolerance": duty_tolerance(),
        "value_tolerance": value_tolerance_percent(), "alert_hour": getattr(settings, "CUSTOMS_ALERT_HOUR", 7),
        "today": today, "org_tz": org.timezone or "UTC"})
