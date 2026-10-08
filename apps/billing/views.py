"""Settings > Billing (admins), Stripe Checkout and Customer Portal, the Stripe webhook, and the onboarding
checklist's dismiss button."""
from __future__ import annotations

import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.core.permissions import require
from apps.core.utils import audit, current_org

from . import onboarding, plans, state, stripe_api, stripe_webhooks
from .models import BillingAccount, StripeEvent
from .usage import usage_for

log = logging.getLogger(__name__)
MAX_WEBHOOK_BYTES = 1024 * 1024


def _admin_org(request):
    org = current_org(request)
    require(request.user, org, "manage")
    return org


@login_required
def billing_settings(request):
    org = _admin_org(request)
    if request.GET.get("checkout") == "done" and request.GET.get("session_id"):
        return _checkout_return(request, org, request.GET["session_id"])
    account = BillingAccount.objects.filter(organization=org).first()
    limits = plans.limits_for(org, account)
    usage = usage_for(org, account, limits)
    return render(request, "billing/settings.html", {
        "enabled": settings.BILLING_ENABLED, "account": account, "limits": limits, "usage": usage,
        "plans": plans.all_plans(), "current_plan": plans.get_plan(account.plan) if account else None,
        "stripe_ready": stripe_api.configured(), "hard_percent": settings.BILLING_HARD_LIMIT_PERCENT,
        "events": StripeEvent.objects.filter(organization=org)[:10],
        "currency": settings.BILLING_CURRENCY, "trial_documents": settings.BILLING_TRIAL_DOCUMENTS,
    })


def _checkout_return(request, org, session_id: str):
    """Back from Stripe Checkout: read the session and subscription now instead of waiting for the webhook."""
    account = BillingAccount.objects.filter(organization=org).first()
    try:
        session = stripe_api.retrieve_checkout_session(session_id)
        if account is None or str(session.get("client_reference_id") or "") != str(org.pk):
            raise stripe_api.StripeError("That checkout belongs to another organization.")
        if session.get("customer") and not account.stripe_customer_id:
            account.stripe_customer_id = str(session["customer"])
            account.save(update_fields=["stripe_customer_id", "updated_at"])
        sub_id = session.get("subscription")
        if isinstance(sub_id, dict):
            sub_id = sub_id.get("id")
        if sub_id:
            outcome = state.apply_subscription(account, stripe_api.retrieve_subscription(str(sub_id)), timezone.now())
            audit(org, "billing.checkout_completed", account, actor=request.user, plan=account.plan,
                  status=account.status, outcome=outcome)
        plan = plans.get_plan(account.plan)
        messages.success(request, f"Thank you. {org.name} is on the {plan.name if plan else 'new'} plan."
                         if account.status in (BillingAccount.Status.ACTIVE, BillingAccount.Status.TRIALING)
                         else "Thank you. Stripe is still confirming the payment; this page updates when it does.")
    except stripe_api.StripeError as e:
        messages.warning(request, f"Your payment went through Stripe, but ShipMatch couldn't confirm it yet ({e}). "
                                  "It updates automatically within a few minutes.")
    return redirect("billing:settings")


@login_required
@require_POST
def checkout(request):
    org = _admin_org(request)
    if not settings.BILLING_ENABLED:
        messages.error(request, "Billing is switched off on this server.")
        return redirect("billing:settings")
    plan = plans.get_plan(request.POST.get("plan"))
    if plan is None or not plan.can_checkout:
        messages.error(request, "That plan isn't available. Choose one of the plans on this page.")
        return redirect("billing:settings")
    # An organization billed outside ShipMatch gets an account here; it has no limits (is_billed is False) until
    # Stripe reports a subscription.
    account, _ = BillingAccount.objects.get_or_create(
        organization=org, defaults={"status": BillingAccount.Status.CANCELED, "plan": ""})
    if account.has_subscription and account.stripe_status in state.LIVE:
        # One subscription per organization: plan changes go through the portal (Stripe prorates them).
        return _portal_redirect(request, org, account)
    base = request.build_absolute_uri(reverse("billing:settings"))
    try:
        url = stripe_api.create_checkout_session(account, plan, request.user,
                                                 success_url=f"{base}?checkout=done&session_id={{CHECKOUT_SESSION_ID}}",
                                                 cancel_url=f"{base}?checkout=canceled")
    except stripe_api.StripeError as e:
        messages.error(request, str(e))
        return redirect("billing:settings")
    audit(org, "billing.checkout_started", account, actor=request.user, plan=plan.key)
    return redirect(url)


def _portal_redirect(request, org, account):
    try:
        url = stripe_api.create_portal_session(account, request.build_absolute_uri(reverse("billing:settings")))
    except stripe_api.StripeError as e:
        messages.error(request, str(e))
        return redirect("billing:settings")
    audit(org, "billing.portal_opened", account, actor=request.user)
    return redirect(url)


@login_required
@require_POST
def portal(request):
    org = _admin_org(request)
    account = BillingAccount.objects.filter(organization=org).first()
    if account is None or not account.stripe_customer_id:
        messages.error(request, "There is no subscription to manage yet. Choose a plan first.")
        return redirect("billing:settings")
    return _portal_redirect(request, org, account)


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Stripe calls this; it is verified by signature, never by session or CSRF."""
    if not settings.BILLING_ENABLED:
        return HttpResponse("Billing is switched off.", status=404)
    payload = request.body
    if len(payload) > MAX_WEBHOOK_BYTES:
        return HttpResponse("Too large.", status=413)
    try:
        stripe_webhooks.verify_signature(payload, request.META.get("HTTP_STRIPE_SIGNATURE", ""),
                                         settings.STRIPE_WEBHOOK_SECRETS, settings.STRIPE_WEBHOOK_TOLERANCE)
    except stripe_webhooks.SignatureError as e:
        log.warning("Stripe webhook refused: %s", e)
        return HttpResponseBadRequest(str(e))
    try:
        event = json.loads(payload)
        if not isinstance(event, dict):
            raise ValueError
    except ValueError:
        return HttpResponseBadRequest("Not JSON.")
    try:
        new, outcome = stripe_webhooks.process(event)
    except ValueError as e:
        return HttpResponseBadRequest(str(e))
    except Exception:
        log.exception("Stripe event %s failed", event.get("id"))
        return HttpResponse("Error; Stripe will retry.", status=500)
    return HttpResponse(outcome if new else "Already processed.", content_type="text/plain")


@login_required
@require_POST
def dismiss_onboarding(request):
    org = _admin_org(request)
    if onboarding.dismiss(org):
        audit(org, "onboarding.dismissed", org, actor=request.user)
        messages.info(request, "The getting-started list is hidden. Everything on it stays in Settings and Team.")
    return redirect("core:dashboard")
