"""{% usage_banner %} on every page and {% onboarding_checklist %} on the dashboard."""
from __future__ import annotations

from django import template
from django.conf import settings

from apps.core.permissions import has_perm

register = template.Library()


def _date(value) -> str:
    from django.utils import timezone

    local = timezone.localtime(value)
    return f"{local.day} {local:%b %Y}"


def banner_for(org, user) -> dict | None:
    """What the team should know about the plan right now (trial ending, payment, usage), or None."""
    if org is None or not settings.BILLING_ENABLED:
        return None
    from apps.billing.plans import account_for
    from apps.billing.usage import cached_usage

    account = account_for(org)
    if account is None or not account.is_billed:
        return None
    admin = has_perm(user, org, "manage")
    fix = " An admin can choose a plan in Settings, Billing." if not admin else ""
    keep = " Review, approval and posting still work."
    if account.trial_over:
        return {"tone": "error", "admin": admin, "action": "Choose a plan",
                "text": f"The free trial ended on {_date(account.trial_ends_at)}, so new documents are paused.{keep}{fix}"}
    if account.status == account.Status.CANCELED:
        return {"tone": "error", "admin": admin, "action": "Choose a plan",
                "text": f"The subscription is canceled, so new documents are paused.{keep}{fix}"}
    if account.status == account.Status.PAST_DUE:
        return {"tone": "warning", "admin": admin, "action": "Update payment",
                "text": "The last payment didn't go through. Stripe will try again; update the card so nothing "
                        "is paused." + ("" if admin else " Let an admin know.")}
    usage = cached_usage(org)
    if usage is not None and usage.hard_reached:
        if usage.trial:
            return {"tone": "error", "admin": admin, "action": "Choose a plan",
                    "text": f"{usage.used:,} documents received, the most the free trial includes, so new documents "
                            f"are paused.{keep}{fix}"}
        return {"tone": "error", "admin": admin, "action": "Choose a bigger plan",
                "text": f"{usage.used:,} documents received this billing month, {usage.percent}% of the plan's "
                        f"{usage.allowance:,}. New documents are paused until {_date(usage.period_end)}.{keep}{fix}"}
    if usage is not None and usage.soft_reached and usage.trial:
        return {"tone": "warning", "admin": admin, "action": "Choose a plan",
                "text": f"{usage.used:,} of the {usage.allowance:,} documents in the free trial used. New documents "
                        f"pause at {usage.hard_limit:,}."}
    if usage is not None and usage.soft_reached:
        return {"tone": "warning", "admin": admin, "action": "See plans",
                "text": f"{usage.used:,} of {usage.allowance:,} documents used this billing month. New documents "
                        f"keep coming in until {usage.hard_limit:,}, then pause until {_date(usage.period_end)}."}
    days = account.trial_days_left
    if days is not None and not account.has_subscription and days <= 3:
        return {"tone": "info", "admin": admin, "action": "Choose a plan",
                "text": f"The free trial ends in {days} day{'s' if days != 1 else ''}, on "
                        f"{_date(account.trial_ends_at)}. Choose a plan to keep documents coming in." +
                        ("" if admin else " Let an admin know.")}
    return None


@register.inclusion_tag("billing/_banner_inner.html", takes_context=True)
def usage_banner(context):
    request = context.get("request")
    org = getattr(request, "org", None) if request is not None else None
    user = getattr(request, "user", None)
    try:
        banner = banner_for(org, user) if user is not None and user.is_authenticated else None
    except Exception:  # a banner must never break a page
        banner = None
    return {"banner": banner}


@register.inclusion_tag("billing/_onboarding_inner.html", takes_context=True)
def onboarding_checklist(context):
    request = context.get("request")
    org = getattr(request, "org", None) if request is not None else None
    user = getattr(request, "user", None)
    if org is None or user is None or not has_perm(user, org, "manage"):
        return {"checklist": None}
    from apps.billing.onboarding import checklist

    return {"checklist": checklist(org), "csrf_token": context.get("csrf_token")}


@register.simple_tag
def billing_enabled() -> bool:
    return bool(settings.BILLING_ENABLED)


@register.simple_tag
def signup_open() -> bool:
    return bool(settings.SIGNUP_ENABLED)
