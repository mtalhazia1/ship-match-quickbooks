"""Small helpers shared by every app."""
from __future__ import annotations

from typing import Any

from django.http import Http404

from .context import client_ip_var, request_id_var
from .models import AuditEvent, Organization


def audit(organization: Organization | None, action: str, obj: Any, actor=None, **data: Any) -> AuditEvent:
    """Write one audit row. Call this for every state change a client may ask about later."""
    return AuditEvent.objects.create(
        organization=organization,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        object_type=obj.__class__.__name__,   # not type(obj): a lazy request.user/org would log "SimpleLazyObject"
        object_id=str(getattr(obj, "pk", obj)),
        data=_jsonable(data),
        ip=client_ip_var.get(),
        request_id=request_id_var.get()[:40],
    )


def clamp_money(value, max_digits: int = 14, decimal_places: int = 2):
    """A decimal column value that is always safe to store and read back.

    SQLite keeps a number that is too big for a DecimalField as a float, and reading it later raises
    decimal.InvalidOperation on every page that touches the column. So an amount computed from a bad value
    (a typo, a misread scan) is rounded to the column's precision and capped at its largest value; NaN and
    infinity become None."""
    from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

    if value is None:
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    step = Decimal(1).scaleb(-decimal_places)
    biggest = Decimal(10) ** (max_digits - decimal_places) - step
    if abs(number) > biggest:   # compared before rounding: quantizing a huge number would itself raise
        return biggest if number > 0 else -biggest
    return number.quantize(step, rounding=ROUND_HALF_UP)


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def orgs_for_user(user):
    """Organizations a user may access: the ones they are a member of (platform superusers included)."""
    if not getattr(user, "is_authenticated", False):
        return Organization.objects.none()
    return Organization.objects.filter(memberships__user=user)


def current_org(request) -> Organization:
    """The organization the user is working in (switchable with ?org=<slug>)."""
    org = getattr(request, "org", None)
    if org is None:
        raise Http404("You are not a member of any organization yet. Ask an admin to invite you.")
    return org


def use_org(request, org: Organization) -> None:
    """Make an object's organization the current one (after access was checked).

    The two-factor middleware only checks the organization that was current when the request arrived, so the
    rule of the organization switched to here is checked again: a member without two-factor can't act in an
    organization that requires it by opening one of its objects from another organization."""
    request.org = org
    request.session["org"] = org.slug
    from django.core.exceptions import PermissionDenied

    from .permissions import mfa_missing

    if mfa_missing(getattr(request, "user", None), org):
        raise PermissionDenied(f"{org.name} requires two-factor authentication. Set it up under Security and "
                               "sign-in, then try again.")
