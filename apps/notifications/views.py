"""Settings > Alerts: channels, which events each one gets, test messages and the delivery log. Admins only."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.permissions import require
from apps.core.utils import audit, current_org

from . import events
from .delivery import attempt
from .models import Channel, Delivery, NotificationSettings
from .webhooks import WebhookURLError, check_webhook_url

MAX_RECIPIENTS = 20


def _org(request):
    org = current_org(request)
    require(request.user, org, "manage")
    return org


@login_required
def alert_settings(request):
    org = _org(request)
    s = NotificationSettings.for_org(org)
    if request.method == "POST":
        raw = request.POST.get("digest_hour", "")
        if not raw.isdigit() or not 0 <= int(raw) <= 23:
            messages.error(request, "Choose an hour between 00:00 and 23:00 for the daily summary.")
            return redirect("notifications:settings")
        before = s.digest_hour
        s.digest_hour = int(raw)
        s.save(update_fields=["digest_hour", "updated_at"])
        audit(org, "notification_settings.updated", s, actor=request.user, hour=f"{s.digest_hour:02d}:00",
              changes={"digest_hour": [before, s.digest_hour]})
        messages.success(request, f"The daily summary is sent at {s.digest_hour:02d}:00 {org.timezone}.")
        return redirect("notifications:settings")
    channels = list(Channel.objects.filter(organization=org))
    last = {}
    for d in Delivery.objects.filter(organization=org).order_by("-created_at")[:300]:
        last.setdefault(d.channel_id, d)
    for c in channels:
        c.last_delivery = last.get(c.pk)
    return render(request, "notifications/settings.html", {
        "channels": channels, "s": s, "hours": range(24),
        "deliveries": Delivery.objects.filter(organization=org).select_related("channel")[:30],
        "event_list": events.EVENTS,
    })


def _clean(request, channel: Channel | None) -> dict:
    p = request.POST
    kind = channel.kind if channel else p.get("kind", "")
    if kind not in Channel.Kind.values:
        raise ValueError("Choose where alerts go: Slack, Microsoft Teams or email.")
    name = p.get("name", "").strip()[:100]
    if not name:
        raise ValueError("Give the channel a name, for example “AP team in Slack”.")
    chosen = [e for e in p.getlist("events") if e in events.EVENT_KEYS]
    if not chosen:
        raise ValueError("Choose at least one alert for this channel.")
    out = {"kind": kind, "name": name, "events": chosen, "enabled": p.get("enabled") == "on"}
    if kind == Channel.Kind.EMAIL:
        recipients = [a.strip() for a in p.get("email_recipients", "").replace(";", ",").replace("\n", ",").split(",")
                      if a.strip()]
        if not recipients:
            raise ValueError("Add at least one email address.")
        if len(recipients) > MAX_RECIPIENTS:
            raise ValueError(f"Use at most {MAX_RECIPIENTS} addresses, or a shared mailbox.")
        for a in recipients:
            try:
                validate_email(a)
            except ValidationError:
                raise ValueError(f"“{a}” is not a valid email address.")
        out["email_recipients"] = ", ".join(dict.fromkeys(recipients))
    else:
        url = p.get("webhook_url", "").strip()
        if url or channel is None:
            out["webhook_url"] = check_webhook_url(kind, url)  # raises WebhookURLError (a ValueError)
    return out


@login_required
def channel_form(request, pk=None):
    org = _org(request)
    channel = get_object_or_404(Channel, pk=pk, organization=org) if pk else None
    kind = channel.kind if channel else request.GET.get("kind", request.POST.get("kind", "slack"))
    kind = kind if kind in Channel.Kind.values else "slack"
    if request.method == "POST":
        try:
            data = _clean(request, channel)
        except ValueError as e:
            messages.error(request, str(e))
            return render(request, "notifications/channel_form.html", {
                "channel": channel, "kind": kind, "kinds": Channel.Kind.choices, "event_list": events.EVENTS,
                "chosen": request.POST.getlist("events"), "url_error": isinstance(e, WebhookURLError),
                "initial": {"name": request.POST.get("name", ""), "enabled": request.POST.get("enabled") == "on",
                            "email_recipients": request.POST.get("email_recipients", "")},
            }, status=400)
        created = channel is None
        if created:
            channel = Channel(organization=org, created_by=request.user)
        url_changed = "webhook_url" in data and data["webhook_url"] != channel.webhook_url
        for k, v in data.items():
            setattr(channel, k, v)
        channel.save()
        audit(org, "notification_channel.created" if created else "notification_channel.updated", channel,
              actor=request.user, name=channel.name, kind=channel.get_kind_display(), events=channel.events,
              enabled=channel.enabled, address_changed=url_changed)  # never log the webhook address itself
        messages.success(request, f"Saved {channel.name}. Send a test message to check it works.")
        return redirect("notifications:settings")
    return render(request, "notifications/channel_form.html", {
        "channel": channel, "kind": kind, "kinds": Channel.Kind.choices, "event_list": events.EVENTS,
        "chosen": channel.events if channel else events.DEFAULT_EVENTS[kind],
        "initial": {"name": channel.name if channel else "", "enabled": channel.enabled if channel else True,
                    "email_recipients": channel.email_recipients if channel else ""},
    })


@login_required
@require_POST
def test_channel(request, pk):
    org = _org(request)
    channel = get_object_or_404(Channel, pk=pk, organization=org)
    msg = events.channel_test_message(channel)
    d = Delivery.objects.create(organization=org, channel=channel, event=events.TEST, title=msg.title,
                                message=msg.as_dict(), is_test=True, object_type="Channel", object_id=str(channel.pk))
    attempt(d, allow_retry=False)
    audit(org, "notification_channel.tested", channel, actor=request.user, name=channel.name, status=d.status,
          http_status=d.http_status)
    if d.status == Delivery.Status.SENT:
        where = "the email addresses" if channel.kind == Channel.Kind.EMAIL else channel.get_kind_display()
        messages.success(request, f"Test message delivered to {where}"
                         + (f" (HTTP {d.http_status})." if d.http_status else "."))
    else:
        messages.error(request, f"The test message to {channel.name} failed. {d.error}")
    return redirect("notifications:settings")


@login_required
@require_POST
def delete_channel(request, pk):
    org = _org(request)
    channel = get_object_or_404(Channel, pk=pk, organization=org)
    audit(org, "notification_channel.deleted", channel, actor=request.user, name=channel.name,
          kind=channel.get_kind_display())
    name = channel.name
    channel.delete()
    messages.success(request, f"Removed {name}. It gets no more alerts.")
    return redirect("notifications:settings")
