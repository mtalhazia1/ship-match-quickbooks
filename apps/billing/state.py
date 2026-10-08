"""The subscription state machine: Stripe's subscription status mapped onto the organization's account.

    trialing ──► active ──► past_due ──► active
        │           │           │
        └───────────┴───────────┴──► canceled ──► (a new subscription) trialing / active

Stripe is the source of truth, but its webhooks can arrive late, twice or out of order, so:
* an event older than the newest state already applied for the same subscription is ignored;
* a subscription Stripe reports as canceled never comes back (that is final in Stripe);
* events about an older subscription never override a newer live one (cancel A, subscribe B, then a late
  "A deleted" arrives: the account stays on B);
* "incomplete" (Checkout still waiting for 3-D Secure) changes nothing yet.
"""
from __future__ import annotations

from datetime import UTC, datetime

from django.utils import timezone

from .models import BillingAccount
from .plans import plan_for_price

S = BillingAccount.Status

# Stripe subscription status -> account status (None = leave the account as it is)
STATUS_MAP = {
    "trialing": S.TRIALING,
    "active": S.ACTIVE,
    "past_due": S.PAST_DUE,
    "unpaid": S.PAST_DUE,
    "canceled": S.CANCELED,
    "incomplete_expired": None,
    "incomplete": None,
    "paused": S.CANCELED,
}
LIVE = {"trialing", "active", "past_due", "unpaid"}
FINAL = {"canceled", "incomplete_expired"}

# Allowed moves of the account's own status. A canceled account comes back only with a live subscription.
TRANSITIONS = {
    S.TRIALING: {S.TRIALING, S.ACTIVE, S.PAST_DUE, S.CANCELED},
    S.ACTIVE: {S.ACTIVE, S.PAST_DUE, S.CANCELED, S.TRIALING},
    S.PAST_DUE: {S.PAST_DUE, S.ACTIVE, S.CANCELED},
    S.CANCELED: {S.CANCELED, S.ACTIVE, S.TRIALING, S.PAST_DUE},
}


def _ts(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC) if value not in (None, "") else None
    except (TypeError, ValueError, OverflowError):
        return None


def _first_item(sub: dict) -> dict:
    items = ((sub.get("items") or {}).get("data") or []) if isinstance(sub.get("items"), dict) else []
    return items[0] if items and isinstance(items[0], dict) else {}


def subscription_price_id(sub: dict) -> str:
    price = _first_item(sub).get("price") or {}
    if isinstance(price, dict):
        return str(price.get("id") or "")
    return str(price or "")


def subscription_period(sub: dict) -> tuple[datetime | None, datetime | None]:
    """Stripe moved the period from the subscription to its items in newer API versions; read either."""
    item = _first_item(sub)
    start = _ts(sub.get("current_period_start")) or _ts(item.get("current_period_start"))
    end = _ts(sub.get("current_period_end")) or _ts(item.get("current_period_end"))
    return start, end


def apply_subscription(account: BillingAccount, sub: dict, seen_at: datetime | None = None) -> str:
    """Bring the account in line with a Stripe subscription object. Returns what happened (for the event log).

    seen_at: when this state was true (the event's creation time, or now for a fresh read from the API).
    """
    seen_at = seen_at or timezone.now()
    sub_id = str(sub.get("id") or "")
    stripe_status = str(sub.get("status") or "")
    if not sub_id or stripe_status not in STATUS_MAP:
        return f"ignored: unknown subscription status {stripe_status or 'missing'}"
    same = sub_id == account.stripe_subscription_id

    if same:
        if account.stripe_synced_at and seen_at < account.stripe_synced_at:
            return "ignored: older than the state already applied"
        if account.stripe_status in FINAL and stripe_status not in FINAL:
            return "ignored: subscription already ended"
    else:
        # Not the subscription on file (or none on file yet): only a live subscription may take its place.
        if stripe_status not in LIVE:
            return "ignored: not the organization's current subscription"
        if (account.stripe_subscription_id and account.stripe_status in LIVE and account.stripe_synced_at
                and seen_at < account.stripe_synced_at):
            return "ignored: a newer subscription is already live"

    new_status = STATUS_MAP[stripe_status]
    old_status = account.status
    if new_status is not None:
        if new_status not in TRANSITIONS.get(old_status, set()):
            return f"ignored: {old_status} can't become {new_status}"
        account.status = new_status

    account.stripe_subscription_id = sub_id
    account.stripe_status = stripe_status
    account.stripe_synced_at = seen_at
    customer = sub.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    if customer and not account.stripe_customer_id:
        account.stripe_customer_id = str(customer)
    plan = plan_for_price(subscription_price_id(sub))
    if plan is not None:
        account.plan = plan.key
    start, end = subscription_period(sub)
    if start and end:
        account.current_period_start, account.current_period_end = start, end
    account.cancel_at_period_end = bool(sub.get("cancel_at_period_end"))
    if stripe_status == "trialing":
        account.trial_ends_at = _ts(sub.get("trial_end")) or account.trial_ends_at
    account.save()
    from .usage import forget_cached_usage

    forget_cached_usage(account.organization_id)
    if new_status is None:
        return f"recorded {stripe_status}; status stays {account.status}"
    return f"{old_status} to {account.status}" if old_status != account.status else f"still {account.status}"
