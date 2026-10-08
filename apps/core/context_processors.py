from django.conf import settings

from .models import Membership
from .permissions import perms_for, role_for
from .utils import orgs_for_user


def app_context(request):
    """Organization, role and permissions for the navigation and buttons in every page."""
    base = {
        "app_version": settings.APP_VERSION,
        "timezone_name": getattr(request, "timezone_name", ""),
        "timezone_source": getattr(request, "timezone_source", "default"),
    }
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return base
    org = getattr(request, "org", None)
    role = role_for(request.user, org)
    match = getattr(request, "resolver_match", None)
    section = SECTIONS.get(f"{match.namespace}:{match.url_name}", match.namespace) if match else ""
    return {
        **base,
        "section": section,
        "org": org,
        "orgs": orgs_for_user(request.user).order_by("name"),
        "role": role,
        "role_label": dict(Membership.Role.choices).get(role, ""),
        "can": perms_for(request.user, org),
        "nav": _nav_counts(org) if org else {},
    }


def _nav_counts(org) -> dict:
    from apps.documents.models import Document
    from apps.shipments.models import Shipment

    return {
        "needs_review": Shipment.objects.filter(organization=org, status=Shipment.Status.NEEDS_REVIEW).count(),
        "loose": Document.objects.filter(organization=org, status__in=LOOSE_STATUSES).count(),
    }


LOOSE_STATUSES = ["unmatched", "needs_ocr", "error", "received"]

# Which sidebar item is highlighted for each page.
SECTIONS = {
    "core:dashboard": "dashboard",
    "review:queue": "queue", "review:shipment": "queue", "review:search": "queue",
    "review:documents": "documents", "review:document": "documents",
    "core:audit": "audit",
    "core:accuracy": "accuracy",
    "core:team": "team",
    "core:settings": "settings", "core:api_keys": "settings", "accounting:settings": "settings",
    "accounts:security": "account", "accounts:password_change": "account",
    "mailboxes:index": "settings", "mailboxes:edit": "settings", "mailboxes:imap_new": "settings",
}
