"""Dashboard, audit log, organization settings, API keys and health checks."""
from __future__ import annotations

import csv
import zoneinfo
from datetime import datetime
from datetime import time as dtime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.files.storage import default_storage
from apps.core.paging import Paginator
from django.db import connection
from django.db.models import Q
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounting.services.providers import active_connection
from apps.accounts.models import ApiKey
from apps.accounts.services.apikeys import create_key
from apps.core import csvsafe, timezones
from apps.shipments.labels import describe_action

from . import dashboard as dash
from .models import AuditEvent, Membership
from .permissions import require
from .utils import audit, current_org

# ---------------------------------------------------------------- health


def health(_request):
    """Liveness: the process is up."""
    return JsonResponse({"status": "ok"})


def ready(_request):
    """Readiness: database, cache and file storage all respond."""
    checks = {}
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception as e:  # pragma: no cover - depends on infrastructure
        checks["database"] = f"error: {type(e).__name__}"
    try:
        cache.set("health:ping", "1", 10)
        checks["cache"] = "ok" if cache.get("health:ping") == "1" else "error: read back failed"
    except Exception as e:  # pragma: no cover
        checks["cache"] = f"error: {type(e).__name__}"
    try:
        default_storage.exists("health-check")
        checks["storage"] = "ok"
    except Exception as e:  # pragma: no cover
        checks["storage"] = f"error: {type(e).__name__}"
    healthy = all(v == "ok" for v in checks.values())
    return JsonResponse({"status": "ok" if healthy else "degraded", "checks": checks}, status=200 if healthy else 503)


# ---------------------------------------------------------------- dashboard


@login_required
def dashboard(request):
    org = current_org(request)
    require(request.user, org, "view")
    days = int(request.GET.get("days", 30)) if request.GET.get("days", "30") in {"7", "30", "90"} else 30
    return render(request, "dashboard.html", {"d": dash.build(org, days), "days": days,
                                              "qbo": active_connection(org)})


# ---------------------------------------------------------------- accuracy


@login_required
def accuracy(request):
    from . import accuracy as acc

    org = current_org(request)
    require(request.user, org, "audit")
    days = int(request.GET.get("days", 90)) if request.GET.get("days", "90") in {"30", "90", "365"} else 90
    reviewed_only = request.GET.get("scope", "reviewed") != "all"
    return render(request, "reports/accuracy.html", {"r": acc.build(org, days, reviewed_only), "days": days,
                                                     "scope": "reviewed" if reviewed_only else "all"})


# ---------------------------------------------------------------- audit log


def _audit_queryset(request, org):
    members = Membership.objects.filter(organization=org)
    member_ids = list(members.values_list("user_id", flat=True))
    usernames = list(members.values_list("user__username", flat=True))
    # Organization events, plus sign-in and security events of this organization's members.
    qs = AuditEvent.objects.filter(
        Q(organization=org)
        | Q(organization__isnull=True, actor_id__in=member_ids)
        | Q(organization__isnull=True, actor__isnull=True, object_type="User", object_id__in=[str(i) for i in member_ids])
        | Q(organization__isnull=True, actor__isnull=True, object_type="str", object_id__in=usernames))
    f = request.GET
    if f.get("actor"):
        qs = qs.filter(actor_id=f["actor"])
    if f.get("action"):
        qs = qs.filter(action__startswith=f["action"])
    if f.get("q"):
        qs = qs.filter(Q(object_id__icontains=f["q"]) | Q(action__icontains=f["q"]) | Q(request_id=f["q"]))
    tz = timezone.get_current_timezone()
    for key, lookup, t in (("from", "created_at__gte", dtime.min), ("to", "created_at__lte", dtime.max)):
        if f.get(key):
            try:
                day = datetime.strptime(f[key], "%Y-%m-%d").date()
                qs = qs.filter(**{lookup: datetime.combine(day, t, tzinfo=tz)})
            except ValueError:
                pass
    return qs.select_related("actor").order_by("-created_at", "-id")


def _describe(e: AuditEvent) -> str:
    return describe_action(e.action, e.data)


AUDIT_ACTION_GROUPS = [
    ("auth.", "Sign-in and security"), ("document.", "Documents"), ("field.", "Field corrections"),
    ("issue.", "Accepted issues"), ("shipment.", "Shipments"), ("bill.", "Accounting"),
    ("team.", "Team"), ("settings.", "Settings"), ("api_key.", "API keys"), ("qbo.", "QuickBooks"),
]


@login_required
def audit_log(request):
    org = current_org(request)
    require(request.user, org, "audit")
    qs = _audit_queryset(request, org)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    for e in page:
        e.description = _describe(e)
    members = Membership.objects.filter(organization=org).select_related("user").order_by("user__username")
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "audit/list.html", {"page": page, "members": members, "groups": AUDIT_ACTION_GROUPS,
                                               "f": request.GET, "query": params.urlencode()})


class _Echo:
    def write(self, value):
        return value


@login_required
def audit_export(request):
    org = current_org(request)
    require(request.user, org, "audit")
    qs = _audit_queryset(request, org)
    writer = csv.writer(_Echo())
    tzname = timezone.get_current_timezone_name()

    def rows():
        yield writer.writerow([f"time ({tzname})", "user", "action", "description", "object", "object id", "ip",
                               "request id", "details"])
        for e in qs.iterator(chunk_size=500):
            yield writer.writerow(csvsafe.row([
                timezone.localtime(e.created_at).strftime("%Y-%m-%d %H:%M:%S"),
                e.actor.get_username() if e.actor else (e.data or {}).get("username") or "system", e.action, _describe(e), e.object_type, e.object_id,
                e.ip or "", e.request_id, str(e.data),
            ]))

    audit(org, "audit.exported", org, actor=request.user, filters=dict(request.GET.items()))
    response = StreamingHttpResponse(rows(), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="shipmatch-audit-{org.slug}-{timezone.localdate()}.csv"'
    return response


# ---------------------------------------------------------------- organization settings


def _parse_rates(raw: str) -> dict:
    """'EUR=1.08, GBP=1.27' -> {'EUR': '1.08', 'GBP': '1.27'}"""
    rates = {}
    for part in (raw or "").replace(";", ",").replace("\n", ",").split(","):
        if not part.strip():
            continue
        cur, _, value = part.partition("=")
        cur = cur.strip().upper()
        if len(cur) != 3 or not cur.isalpha():
            raise ValueError(f"'{part.strip()}' should look like EUR=1.08")
        try:
            rate = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"'{part.strip()}' should look like EUR=1.08")
        if rate <= 0:
            raise ValueError(f"Rate for {cur} must be positive")
        rates[cur] = str(rate)
    return rates


@login_required
def org_settings(request):
    org = current_org(request)
    require(request.user, org, "manage")
    zones = timezones.choices(org.timezone)
    if request.method == "POST":
        p = request.POST
        try:
            name = p.get("name", "").strip()
            if not name:
                raise ValueError("Organization name can't be empty.")
            if len(name) > org._meta.get_field("name").max_length:
                raise ValueError(f"Organization name can be at most {org._meta.get_field('name').max_length} "
                                 f"characters (you typed {len(name)}).")
            currency = p.get("home_currency", "").strip().upper()
            if len(currency) != 3 or not currency.isalpha():
                raise ValueError("Home currency must be a 3-letter code such as USD.")
            tz = p.get("timezone", "UTC")
            if tz not in zoneinfo.available_timezones():
                raise ValueError("Choose a valid time zone.")
            threshold = p.get("review_threshold", "").strip()
            threshold = float(threshold) if threshold else None
            if threshold is not None and not 0.5 <= threshold <= 1.0:
                raise ValueError("Review threshold must be between 0.50 and 1.00.")
            rates = _parse_rates(p.get("fx_rates", ""))
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("core:settings")
        tracked = ("name", "home_currency", "timezone", "review_threshold", "fx_rates", "require_mfa", "maker_checker")
        before = {k: getattr(org, k) for k in tracked}
        org.name, org.home_currency, org.timezone, org.review_threshold, org.fx_rates = name, currency, tz, threshold, rates
        org.require_mfa = p.get("require_mfa") == "on"
        org.maker_checker = p.get("maker_checker") == "on"
        org.save()
        after = {k: getattr(org, k) for k in tracked}
        audit(org, "settings.updated", org, actor=request.user,
              changes={k: [before[k], after[k]] for k in before if before[k] != after[k]})
        messages.success(request, "Settings saved.")
        return redirect("core:settings")
    rates_text = ", ".join(f"{k}={v}" for k, v in (org.fx_rates or {}).items())
    return render(request, "settings/org.html", {"zones": zones, "rates_text": rates_text,
                                                 "qbo": active_connection(org)})


# ---------------------------------------------------------------- API keys


@login_required
def api_keys(request):
    org = current_org(request)
    require(request.user, org, "manage")
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        role = request.POST.get("role", ApiKey.Role.VIEWER)
        days = request.POST.get("days", "")
        if not name or role not in ApiKey.Role.values:
            messages.error(request, "Give the key a name and choose its access.")
            return redirect("core:api_keys")
        from apps.integrations.apiscopes import scopes_from_post

        scopes = scopes_from_post(request.POST, role)
        if scopes is None:
            messages.error(request, "Choose at least one thing the key may read, or give it upload access.")
            return redirect("core:api_keys")
        key, token = create_key(org, name, role, request.user, int(days) if days.isdigit() else None, scopes=scopes)
        audit(org, "api_key.created", key, actor=request.user, name=name, role=role, prefix=key.prefix,
              scopes=key.scope_labels)
        request.session["new_api_token"] = {"name": name, "token": token}
        return redirect("core:api_keys")
    return render(request, "settings/api_keys.html", {
        "keys": ApiKey.objects.filter(organization=org).select_related("created_by"),
        "roles": ApiKey.Role.choices, "new_token": request.session.pop("new_api_token", None),
    })


@login_required
@require_POST
def revoke_api_key(request, pk):
    org = current_org(request)
    require(request.user, org, "manage")
    key = get_object_or_404(ApiKey, pk=pk, organization=org)
    if key.revoked_at is None:
        key.revoked_at = timezone.now()
        key.save(update_fields=["revoked_at"])
        audit(org, "api_key.revoked", key, actor=request.user, name=key.name, prefix=key.prefix)
        messages.success(request, f"Revoked {key.name}. Requests using it now fail.")
    return redirect("core:api_keys")
