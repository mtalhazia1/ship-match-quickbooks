"""Stripe webhooks: signature check and event handling.

Stripe-Signature: t=<unix time>,v1=<hex>[,v1=<hex>...][,v0=...]
v1 = HMAC-SHA256(signing secret, "<t>.<raw body>"). Any v1 may match any configured secret (Stripe sends
several while a secret is being rolled); the timestamp must be within STRIPE_WEBHOOK_TOLERANCE seconds.
Each event id is processed once (StripeEvent); a failure rolls back and answers 500 so Stripe resends it.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import UTC, datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.models import Organization
from apps.core.utils import audit

from . import state, stripe_api
from .models import BillingAccount, StripeEvent

log = logging.getLogger(__name__)


class SignatureError(ValueError):
    pass


def compute_signature(secret: str, timestamp: int, payload: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()


def verify_signature(payload: bytes, header: str, secrets: list[str], tolerance: int, now: float | None = None) -> int:
    """Return the signed timestamp, or raise SignatureError."""
    secrets = [s for s in secrets if s]
    if not secrets:
        raise SignatureError("No webhook signing secret is configured (STRIPE_WEBHOOK_SECRET).")
    timestamp, signatures = None, []
    for part in (header or "").split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            continue
        if key == "t" and timestamp is None:
            timestamp = value.strip()
        elif key == "v1":
            signatures.append(value.strip())
    if not timestamp or not signatures:
        raise SignatureError("The Stripe-Signature header is missing or has no v1 signature.")
    try:
        ts = int(timestamp)
    except ValueError:
        raise SignatureError("The Stripe-Signature timestamp isn't a number.")
    expected = [compute_signature(s, ts, payload) for s in secrets]
    if not any(hmac.compare_digest(e, sig) for e in expected for sig in signatures):
        raise SignatureError("The signature doesn't match any configured signing secret.")
    now = time.time() if now is None else now
    if tolerance and abs(now - ts) > tolerance:
        raise SignatureError("The signed timestamp is too old (or in the future); this may be a replayed request.")
    return ts


# --------------------------------------------------------------------------- events


def _account_from(obj: dict) -> BillingAccount | None:
    """Find the organization's account: by Stripe customer first, then by the org id ShipMatch put in metadata."""
    customer = obj.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    if customer:
        account = BillingAccount.objects.select_related("organization").filter(stripe_customer_id=customer).first()
        if account is not None:
            return account
    metadata = obj.get("metadata") or {}
    for raw in (metadata.get("org_id"), obj.get("client_reference_id")):
        if raw and str(raw).isdigit():
            org = Organization.objects.filter(pk=int(raw)).first()
            if org is None:
                continue
            account, _ = BillingAccount.objects.select_related("organization").get_or_create(
                organization=org, defaults={"status": BillingAccount.Status.CANCELED})
            if customer and account.stripe_customer_id and account.stripe_customer_id != customer:
                log.warning("Stripe event for org %s names customer %s, account has %s", org.pk, customer,
                            account.stripe_customer_id)
                return None
            return account
    return None


def _checkout_completed(obj: dict, seen_at: datetime) -> tuple[BillingAccount | None, str]:
    if obj.get("mode") != "subscription":
        return None, "ignored: not a subscription checkout"
    account = _account_from(obj)
    if account is None:
        return None, "ignored: no organization for this checkout"
    customer = obj.get("customer")
    if customer and not account.stripe_customer_id:
        account.stripe_customer_id = str(customer)
        account.save(update_fields=["stripe_customer_id", "updated_at"])
    sub_id = obj.get("subscription")
    if isinstance(sub_id, dict):
        return account, state.apply_subscription(account, sub_id, seen_at)
    if not sub_id:
        return account, "checkout finished without a subscription"
    try:
        sub = stripe_api.retrieve_subscription(str(sub_id))
    except stripe_api.StripeError as e:
        # customer.subscription.created/updated carry the same information; they will apply it.
        return account, f"linked the customer; subscription not read yet ({e})"
    return account, state.apply_subscription(account, sub, timezone.now())


def _subscription_event(obj: dict, seen_at: datetime) -> tuple[BillingAccount | None, str]:
    account = _account_from(obj)
    if account is None:
        return None, "ignored: no organization for this subscription"
    return account, state.apply_subscription(account, obj, seen_at)


def _payment_failed(obj: dict, seen_at: datetime) -> tuple[BillingAccount | None, str]:
    account = _account_from(obj)
    if account is None:
        return None, "ignored: no organization for this invoice"
    amount = ""
    try:
        amount = f"{(obj.get('currency') or '').upper()} {int(obj.get('amount_due') or 0) / 100:,.2f}".strip()
    except (TypeError, ValueError):
        amount = ""
    next_try = state._ts(obj.get("next_payment_attempt"))
    org = account.organization

    def send():
        from .emails import payment_failed_email

        try:
            payment_failed_email(org, amount, f"{next_try:%d %b %Y}" if next_try else "")
        except Exception:
            log.exception("Could not email admins of %s about a failed payment", org.slug)

    transaction.on_commit(send)
    audit(org, "billing.payment_failed", account, amount=amount, invoice=str(obj.get("id") or "")[:80])
    return account, "admins emailed about the failed payment"


HANDLERS = {
    "checkout.session.completed": _checkout_completed,
    "customer.subscription.created": _subscription_event,
    "customer.subscription.updated": _subscription_event,
    "customer.subscription.deleted": _subscription_event,
    "customer.subscription.paused": _subscription_event,
    "customer.subscription.resumed": _subscription_event,
    "invoice.payment_failed": _payment_failed,
}


def process(event: dict) -> tuple[bool, str]:
    """Apply one verified event. Returns (newly processed, outcome). Raises on errors (the caller answers 500)."""
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    if not event_id.startswith("evt_") or not event_type:
        raise ValueError("Not a Stripe event")
    obj = ((event.get("data") or {}).get("object")) or {}
    seen_at = state._ts(event.get("created")) or datetime.now(UTC)
    with transaction.atomic():
        try:
            with transaction.atomic():
                record = StripeEvent.objects.create(event_id=event_id[:80], type=event_type[:80], created=seen_at,
                                                    livemode=bool(event.get("livemode")))
        except IntegrityError:
            return False, "already processed"
        handler = HANDLERS.get(event_type)
        if handler is None:
            account, outcome = None, "ignored: ShipMatch doesn't use this event"
        else:
            account, outcome = handler(obj, seen_at)
        record.organization_id = account.organization_id if account else None
        record.outcome = outcome[:300]
        record.save(update_fields=["organization", "outcome"])
        if account is not None and handler in (_checkout_completed, _subscription_event):
            audit(account.organization, "billing.stripe_event", account, event=event_type, outcome=outcome[:200],
                  status=account.status, plan=account.plan)
    return True, outcome
