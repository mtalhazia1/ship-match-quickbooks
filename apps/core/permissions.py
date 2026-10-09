"""Role-based permissions per organization.

| Permission       | Viewer | Reviewer | Approver | Admin |
|------------------|:------:|:--------:|:--------:|:-----:|
| view             |   x    |    x     |    x     |   x   |
| upload, edit     |        |    x     |    x     |   x   |
| accept_warning   |        |    x     |    x     |   x   |
| override_error   |        |          |    x     |   x   |
| approve, post    |        |          |    x     |   x   |
| audit            |        |          |    x     |   x   |
| manage           |        |          |          |   x   |

Platform superusers act as admins in every organization.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied

from .models import Membership, Organization

ROLE_RANK = {"viewer": 0, "reviewer": 1, "approver": 2, "admin": 3}

PERMISSION_MIN_ROLE = {
    "view": "viewer",
    "upload": "reviewer",
    "edit": "reviewer",
    "accept_warning": "reviewer",
    "override_error": "approver",
    "approve": "approver",
    "post": "approver",
    "audit": "approver",
    "manage": "admin",
}

PERMISSION_TEXT = {
    "view": "view this organization",
    "upload": "upload documents",
    "edit": "edit documents",
    "accept_warning": "accept warnings",
    "override_error": "override errors",
    "approve": "approve or reject shipments",
    "post": "post bills to accounting",
    "audit": "view the audit log and finance reports",
    "manage": "manage team and settings",
}


def membership_for(user, org: Organization) -> Membership | None:
    if not getattr(user, "is_authenticated", False):
        return None
    cache = getattr(user, "_membership_cache", None)
    if cache is None:
        cache = user._membership_cache = {}
    if org.pk not in cache:
        cache[org.pk] = Membership.objects.filter(user=user, organization=org).first()
    return cache[org.pk]


def role_for(user, org: Organization | None) -> str | None:
    if org is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return None
    # Membership only: a platform superuser has no role in an organization they don't belong to. Support access
    # means being added as a member (Django admin), which the organization's audit log records.
    m = membership_for(user, org)
    return m.role if m else None


def has_perm(user, org: Organization | None, perm: str) -> bool:
    role = role_for(user, org)
    return role is not None and ROLE_RANK[role] >= ROLE_RANK[PERMISSION_MIN_ROLE[perm]]


class RoleDenied(PermissionDenied):
    """A role check failed. Carries the organization and permission so the 403 page can audit it (QA-027)."""

    def __init__(self, message: str, org: Organization | None = None, perm: str = ""):
        super().__init__(message)
        self.org, self.perm = org, perm


def require(user, org: Organization | None, perm: str) -> None:
    if not has_perm(user, org, perm):
        raise RoleDenied(f"Your role does not allow you to {PERMISSION_TEXT[perm]}.", org, perm)
    if mfa_missing(user, org):
        # The sign-in middleware checks the organization in use; this covers objects of another organization
        # reached by id (comments, links from alerts, the firm view).
        raise PermissionDenied(f"{org.name} requires two-factor authentication. Set it up under Security and "
                               "sign-in, then try again.")


def mfa_missing(user, org: Organization | None) -> bool:
    """The organization requires two-factor authentication and this signed-in person hasn't turned it on."""
    if org is None or not org.require_mfa or not getattr(user, "is_authenticated", False):
        return False
    profile = getattr(user, "profile", None)
    return not (profile and profile.mfa_enabled)


def perms_for(user, org: Organization | None) -> dict[str, bool]:
    return {p: has_perm(user, org, p) for p in PERMISSION_MIN_ROLE}
