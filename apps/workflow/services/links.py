"""Links in "ready for approval" alerts (email, Slack, Teams).

The link carries a signed, expiring token that only says which shipment the person was sent to approve.
It never signs anyone in: the page needs a normal sign-in (with two-factor when it is on) and membership
of the shipment's organization, and opening it changes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.core import signing
from django.urls import reverse

SALT = "shipmatch.workflow.approval-link"


@dataclass
class LinkTarget:
    org_id: int
    shipment_id: int
    expired: bool = False


def make_token(shipment) -> str:
    return signing.dumps({"o": shipment.organization_id, "s": shipment.pk}, salt=SALT, compress=False)


def approval_path(shipment) -> str:
    return reverse("workflow:approval_link", args=[make_token(shipment)])


def approval_url(shipment) -> str:
    from apps.notifications.events import absolute

    return absolute(approval_path(shipment))


def max_age_seconds() -> int:
    return max(1, int(getattr(settings, "APPROVAL_LINK_MAX_AGE_HOURS", 72))) * 3600


def read_token(token: str) -> LinkTarget:
    """The shipment a token points at. Raises signing.BadSignature when the token was altered or made with
    another key; an authentic but old token comes back with expired=True."""
    expired = False
    try:
        data = signing.loads(token, salt=SALT, max_age=max_age_seconds())
    except signing.SignatureExpired:
        expired = True
        data = signing.loads(token, salt=SALT)  # authentic, just old: still says which shipment
    if not isinstance(data, dict):
        raise signing.BadSignature("Unexpected link contents")
    try:
        return LinkTarget(int(data["o"]), int(data["s"]), expired)
    except (KeyError, TypeError, ValueError):
        raise signing.BadSignature("Unexpected link contents")
