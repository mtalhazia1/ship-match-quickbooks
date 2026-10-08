"""A small Stripe REST client (httpx, form-encoded, no SDK): customers, Checkout, the Customer Portal and
reading subscriptions. Tests replace _TRANSPORT with httpx.MockTransport; nothing here runs on a public demo.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0, connect=5.0)
# Checkout only accepts a trial end at least 48 hours away.
MIN_TRIAL_LEFT = timedelta(hours=48, minutes=5)

# Tests replace this with httpx.MockTransport; None means the real network.
_TRANSPORT: httpx.BaseTransport | None = None


class StripeError(Exception):
    """Stripe refused the request or couldn't be reached. The message is safe to show to an admin."""

    def __init__(self, message: str, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def configured() -> bool:
    return bool(settings.STRIPE_SECRET_KEY)


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def flatten(params: dict, prefix: str = "") -> list[tuple[str, str]]:
    """{"line_items": [{"price": "p", "quantity": 1}]} -> [("line_items[0][price]", "p"), ...] (Stripe's form style)."""
    out: list[tuple[str, str]] = []
    for k, v in params.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if v is None:
            continue
        if isinstance(v, dict):
            out.extend(flatten(v, key))
        elif isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out.extend(flatten(item, f"{key}[{i}]"))
                else:
                    out.append((f"{key}[{i}]", _scalar(item)))
        else:
            out.append((key, _scalar(v)))
    return out


def request(method: str, path: str, params: dict | None = None, idempotency_key: str = "") -> dict:
    from apps.demo.mail import outbound_blocked

    if outbound_blocked():
        raise StripeError("Billing is turned off on the public demo, so nothing is sent to Stripe.")
    if not configured():
        raise StripeError("Stripe isn't set up on this server yet (STRIPE_SECRET_KEY is empty). "
                          "Ask the person who runs ShipMatch to add it.")
    headers = {"Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}",
               "Stripe-Version": settings.STRIPE_API_VERSION,
               "User-Agent": f"ShipMatch/{settings.APP_VERSION} billing"}
    pairs = flatten(params or {})
    try:
        with httpx.Client(base_url=settings.STRIPE_API_BASE, timeout=TIMEOUT, transport=_TRANSPORT,
                          follow_redirects=False) as client:
            if method == "GET":
                response = client.get(path, params=pairs, headers=headers)
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                if idempotency_key:
                    headers["Idempotency-Key"] = idempotency_key
                response = client.request(method, path, content=urlencode(pairs).encode(), headers=headers)
    except httpx.TimeoutException:
        raise StripeError("Stripe didn't answer in time. Try again in a minute.")
    except httpx.TransportError as e:
        raise StripeError(f"Couldn't reach Stripe ({type(e).__name__}). Try again in a minute.")
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        err = err if isinstance(err, dict) else {}
        message = str(err.get("message") or f"Stripe answered HTTP {response.status_code}.")
        log.warning("Stripe %s %s failed: %s %s", method, path, response.status_code, message)
        raise StripeError(f"Stripe refused the request: {message}", response.status_code, str(err.get("code") or ""))
    if not isinstance(data, dict):
        raise StripeError("Stripe sent an answer ShipMatch couldn't read.")
    return data


# --------------------------------------------------------------------------- calls


def ensure_customer(account, user) -> str:
    """The organization's Stripe customer, created the first time."""
    if account.stripe_customer_id:
        return account.stripe_customer_id
    org = account.organization
    customer = request("POST", "/v1/customers", {
        "name": org.name[:200],
        "email": (getattr(user, "email", "") or "")[:200] or None,
        "metadata": {"org_id": org.pk, "org_slug": org.slug},
    }, idempotency_key=f"shipmatch-customer-{org.pk}-{account.pk}")
    account.stripe_customer_id = str(customer.get("id") or "")
    if not account.stripe_customer_id:
        raise StripeError("Stripe didn't return a customer.")
    account.save(update_fields=["stripe_customer_id", "updated_at"])
    return account.stripe_customer_id


def create_checkout_session(account, plan, user, success_url: str, cancel_url: str) -> str:
    """A Checkout page for a monthly subscription to `plan`. Returns the URL to send the admin to."""
    org = account.organization
    customer = ensure_customer(account, user)
    subscription_data: dict = {"metadata": {"org_id": org.pk, "plan": plan.key}}
    if (account.status == account.Status.TRIALING and account.trial_ends_at
            and account.trial_ends_at - timezone.now() > MIN_TRIAL_LEFT):
        # Subscribing during the free trial: the first charge waits until the trial ends.
        subscription_data["trial_end"] = int(account.trial_ends_at.timestamp())
    session = request("POST", "/v1/checkout/sessions", {
        "mode": "subscription",
        "customer": customer,
        "client_reference_id": org.pk,
        "line_items": [{"price": plan.price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "allow_promotion_codes": True,
        "metadata": {"org_id": org.pk, "plan": plan.key},
        "subscription_data": subscription_data,
    }, idempotency_key=f"shipmatch-checkout-{org.pk}-{uuid.uuid4().hex}")
    url = str(session.get("url") or "")
    if not url.startswith("https://"):
        raise StripeError("Stripe didn't return a checkout page.")
    return url


def create_portal_session(account, return_url: str) -> str:
    if not account.stripe_customer_id:
        raise StripeError("There is no subscription to manage yet. Choose a plan first.")
    session = request("POST", "/v1/billing_portal/sessions",
                      {"customer": account.stripe_customer_id, "return_url": return_url},
                      idempotency_key=f"shipmatch-portal-{account.organization_id}-{uuid.uuid4().hex}")
    url = str(session.get("url") or "")
    if not url.startswith("https://"):
        raise StripeError("Stripe didn't return a billing portal page.")
    return url


_ID = re.compile(r"^[a-z]{2,6}_[A-Za-z0-9_]{6,250}$")


def _object_id(value: str, prefix: str) -> str:
    """Ids go into the URL path: only Stripe's own shape (cs_..., sub_...) is accepted."""
    value = (value or "").strip()
    if not value.startswith(prefix + "_") or not _ID.match(value):
        raise StripeError("That isn't a Stripe reference ShipMatch recognises.")
    return value


def retrieve_checkout_session(session_id: str) -> dict:
    return request("GET", f"/v1/checkout/sessions/{_object_id(session_id, 'cs')}")


def retrieve_subscription(subscription_id: str) -> dict:
    return request("GET", f"/v1/subscriptions/{_object_id(subscription_id, 'sub')}")
