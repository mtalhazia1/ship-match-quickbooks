"""Exports (view permission) and Settings > Webhooks (manage permission)."""
from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.core.permissions import require
from apps.core.utils import audit, current_org

from . import delivery, dispatch, events, exports, signing, urlguard
from .models import WebhookDelivery, WebhookEndpoint, WebhookEvent

MAX_ENDPOINTS = 10
LIST_PAGES = {"shipments": "review:queue", "issues": "review:queue", "documents": "review:documents"}

# ---------------------------------------------------------------- exports


@login_required
def export(request, kind: str):
    org = current_org(request)
    require(request.user, org, "view")
    if kind not in exports.KINDS:
        raise Http404("Unknown export.")
    fmt = request.GET.get("format", "csv")
    back = reverse(LIST_PAGES[kind])
    if fmt not in exports.FORMATS:
        messages.error(request, "Choose CSV or Excel for the export.")
        return redirect(back)
    try:
        built = exports.build(org, kind, request.GET)
    except exports.ExportError as e:
        messages.error(request, f"Couldn't export: {e}")
        return redirect(back)
    audit(org, "export.downloaded", org, actor=request.user, kind=kind, format=fmt,
          filters=exports.filters_for_audit(request.GET))
    if fmt == "xlsx":
        return FileResponse(exports.write_xlsx(built), as_attachment=True, filename=f"{built.filename}.xlsx",
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response = StreamingHttpResponse(exports.csv_chunks(built), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{built.filename}.csv"'
    return response


# ---------------------------------------------------------------- webhooks


def _org(request):
    org = current_org(request)
    require(request.user, org, "manage")
    return org


def _allowed(org) -> bool:
    return dispatch.webhooks_allowed(org)


def _clean(request, endpoint: WebhookEndpoint | None) -> dict:
    p = request.POST
    url = urlguard.check_url(p.get("url", ""))
    if endpoint is None or url != endpoint.url:
        urlguard.check_and_resolve(url)  # the host must resolve, to public addresses only
    chosen = [e for e in p.getlist("events") if e in events.EVENT_TYPES]
    if not chosen:
        raise ValueError("Choose at least one event to send.")
    return {"url": url, "description": p.get("description", "").strip()[:200], "events": chosen}


@login_required
def webhooks(request):
    org = _org(request)
    allowed = _allowed(org)
    endpoints = list(WebhookEndpoint.objects.filter(organization=org))
    if request.method == "POST":
        if not allowed:
            messages.error(request, "Outgoing webhooks aren't part of your plan. Choose the Growth or Scale plan "
                                    "in Settings, Billing.")
            return redirect("integrations:webhooks")
        if len(endpoints) >= MAX_ENDPOINTS:
            messages.error(request, f"An organization can have up to {MAX_ENDPOINTS} endpoints. Remove one first.")
            return redirect("integrations:webhooks")
        try:
            data = _clean(request, None)
        except ValueError as e:
            messages.error(request, str(e))
            request.session["webhook_form"] = {"url": request.POST.get("url", "")[:2000],
                                               "description": request.POST.get("description", "")[:200],
                                               "events": request.POST.getlist("events")}
            return redirect("integrations:webhooks")
        secret = signing.new_secret()
        ep = WebhookEndpoint.objects.create(organization=org, secret=secret, created_by=request.user, **data)
        audit(org, "webhook.created", ep, actor=request.user, host=ep.host, events=data["events"])
        request.session["new_webhook_secret"] = {"id": ep.pk, "secret": secret}
        messages.success(request, "Endpoint added. Copy its signing secret now: it is shown only once.")
        return redirect("integrations:webhook_edit", pk=ep.pk)
    last = {}
    for d in WebhookDelivery.objects.filter(endpoint__organization=org).order_by("-created_at")[:300]:
        last.setdefault(d.endpoint_id, d)
    for ep in endpoints:
        ep.last_delivery = last.get(ep.pk)
    form = request.session.pop("webhook_form", None) or {"events": [k for k in events.EVENT_TYPES
                                                                    if k != "document.extracted"]}
    return render(request, "integrations/webhooks.html", {
        "endpoints": endpoints, "allowed": allowed, "event_types": events.choices(), "form": form,
        "deliveries": WebhookDelivery.objects.filter(endpoint__organization=org)
        .select_related("endpoint", "event")[:30],
        "max_endpoints": MAX_ENDPOINTS, "max_attempts": settings.WEBHOOK_MAX_ATTEMPTS,
        "disable_after": settings.WEBHOOK_DISABLE_AFTER_FAILURES, "timeout": settings.WEBHOOK_TIMEOUT_SECONDS,
    })


def _endpoint(request, pk) -> WebhookEndpoint:
    org = _org(request)
    return get_object_or_404(WebhookEndpoint, pk=pk, organization=org)


@login_required
def webhook_edit(request, pk):
    ep = _endpoint(request, pk)
    org = ep.organization
    if request.method == "POST":
        try:
            data = _clean(request, ep)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("integrations:webhook_edit", pk=ep.pk)
        enable = request.POST.get("enabled") == "on"
        before = {"host": ep.host, "events": ep.events, "enabled": ep.enabled, "description": ep.description}
        url_changed = data["url"] != ep.url
        ep.url, ep.description, ep.events = data["url"], data["description"], data["events"]
        if enable and not ep.enabled:
            if not _allowed(org):
                messages.error(request, "Outgoing webhooks aren't part of your plan, so the endpoint stays off.")
                enable = False
            else:
                ep.consecutive_failures, ep.disabled_at, ep.disabled_reason = 0, None, ""
        ep.enabled = enable
        ep.save()
        after = {"host": ep.host, "events": ep.events, "enabled": ep.enabled, "description": ep.description}
        audit(org, "webhook.updated", ep, actor=request.user, host=ep.host, url_changed=url_changed,
              changes={k: [before[k], after[k]] for k in before if before[k] != after[k]})
        messages.success(request, "Endpoint saved." + ("" if ep.enabled else " It is off: nothing is sent to it."))
        return redirect("integrations:webhook_edit", pk=ep.pk)
    new_secret = request.session.get("new_webhook_secret")
    if new_secret and new_secret.get("id") == ep.pk:
        request.session.pop("new_webhook_secret", None)
        secret = new_secret.get("secret")
    else:
        secret = None
    return render(request, "integrations/webhook_edit.html", {
        "ep": ep, "event_types": events.choices(), "new_secret": secret, "allowed": _allowed(org),
        "deliveries": ep.deliveries.select_related("event").order_by("-created_at")[:50],
        "overlap_hours": settings.WEBHOOK_SECRET_OVERLAP_HOURS,
        "previous_active": len(ep.signing_secrets()) > 1,
    })


@login_required
@require_POST
def webhook_rotate(request, pk):
    ep = _endpoint(request, pk)
    secret = signing.new_secret()
    ep.previous_secret, ep.secret = ep.secret, secret
    ep.previous_secret_expires_at = timezone.now() + timezone.timedelta(hours=settings.WEBHOOK_SECRET_OVERLAP_HOURS)
    ep.save(update_fields=["secret", "previous_secret", "previous_secret_expires_at", "updated_at"])
    audit(ep.organization, "webhook.secret_rotated", ep, actor=request.user, host=ep.host)
    request.session["new_webhook_secret"] = {"id": ep.pk, "secret": secret}
    messages.success(request, f"New signing secret created. For the next {settings.WEBHOOK_SECRET_OVERLAP_HOURS} "
                              "hours events are signed with both the new and the old secret, so you can switch the "
                              "receiving system over without losing events.")
    return redirect("integrations:webhook_edit", pk=ep.pk)


@login_required
@require_POST
def webhook_test(request, pk):
    ep = _endpoint(request, pk)
    org = ep.organization
    with transaction.atomic():
        event = WebhookEvent(organization=org, type=events.TEST, object_type="WebhookEndpoint", object_id=str(ep.pk))
        event.payload = events.build_payload(org, events.TEST, {
            "object": "test", "message": f"Test event from ShipMatch for {org.name}.",
            "endpoint_id": ep.pk, "sent_by": request.user.get_full_name() or request.user.get_username(),
        }, event.event_id, timezone.now())
        event.save()
        d = WebhookDelivery.objects.create(endpoint=ep, event=event, is_test=True,
                                           status=WebhookDelivery.Status.SENDING)
    outcome = delivery.attempt(d, allow_retry=False)
    d.refresh_from_db()
    audit(org, "webhook.tested", ep, actor=request.user, host=ep.host,
          result="delivered" if outcome.ok else (d.error[:150] or "failed"), status=d.response_status)
    if outcome.ok:
        messages.success(request, f"Test event delivered: {ep.host} answered HTTP {d.response_status}.")
    else:
        messages.error(request, f"Test event not delivered. {d.error}")
    return redirect("integrations:webhook_edit", pk=ep.pk)


@login_required
@require_POST
def webhook_delete(request, pk):
    ep = _endpoint(request, pk)
    org, host = ep.organization, ep.host
    audit(org, "webhook.deleted", ep, actor=request.user, host=host)
    ep.delete()
    messages.success(request, f"Endpoint for {host} removed. Nothing more is sent to it.")
    return redirect("integrations:webhooks")


@login_required
@require_POST
def delivery_replay(request, pk):
    org = _org(request)
    original = get_object_or_404(WebhookDelivery.objects.select_related("endpoint", "event"), pk=pk,
                                 endpoint__organization=org)
    ep = original.endpoint
    back = request.POST.get("next") or reverse("integrations:webhook_edit", args=[ep.pk])
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        back = reverse("integrations:webhook_edit", args=[ep.pk])
    if not ep.enabled:
        messages.error(request, "The endpoint is off. Turn it on first, then replay.")
        return redirect(back)
    if original.is_test:
        messages.info(request, "Test events aren't replayed. Use Send test event instead.")
        return redirect(back)
    replay = WebhookDelivery.objects.create(endpoint=ep, event=original.event, replay_of=original)
    audit(org, "webhook.replayed", ep, actor=request.user, host=ep.host, event=original.event.event_id,
          type=original.event.type)
    transaction.on_commit(lambda: dispatch.enqueue(replay.pk))
    messages.success(request, f"Sending {original.event.type} ({original.event.event_id}) to {ep.host} again. "
                              "It has the same event id, so the receiving system can tell it's a repeat.")
    return redirect(back)
