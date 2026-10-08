"""Invitations to join an organization for a person who already has a ShipMatch account.

An admin may not attach someone else's account to their organization: that would give them a hold on a
shared account (its password, its two-factor setup). So the existing account's owner has to say yes. The
invitation is a signed, expiring link emailed to the address on the account; it names the organization, the
role and the approval limit, and it only works when that same person is signed in. Nothing is stored until they
accept, so there is nothing to clean up if they never do."""
from __future__ import annotations

from decimal import Decimal

from django.core import signing
from django.urls import reverse

SALT = "shipmatch.org-invitation"
MAX_AGE_SECONDS = 7 * 24 * 3600   # a week


def make_token(org, user, role: str, limit: Decimal | None, invited_by) -> str:
    return signing.dumps({"o": org.pk, "u": user.pk, "r": role, "l": None if limit is None else str(limit),
                          "b": invited_by.pk}, salt=SALT, compress=False)


def read_token(token: str) -> dict:
    """The invitation's contents. Raises signing.BadSignature (SignatureExpired is one) when the link was altered,
    cut short or is older than a week."""
    return signing.loads(token, salt=SALT, max_age=MAX_AGE_SECONDS)


def link_for(request, token: str) -> str:
    return request.build_absolute_uri(reverse("accounts:accept_invite", args=[token]))
