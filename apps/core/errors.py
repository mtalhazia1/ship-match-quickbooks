"""Branded error pages. The 500 page renders without the database or request context."""
import logging

from django.core.cache import cache
from django.http import HttpResponseServerError
from django.shortcuts import render
from django.template import loader
from django.utils.http import url_has_allowed_host_and_scheme

log = logging.getLogger(__name__)


DENIAL_REPEAT_SECONDS = 60   # the same person refused at the same URL is recorded once a minute


def record_denial(request, *, org=None, permission: str = "", reason: str = "", api_key=None) -> None:
    """Audit a refused action (QA-027): who tried what, where. Written to the organization's log only when the
    person (or API key) belongs to it, so a stranger probing ids doesn't put their name in another company's log.
    Never stops the 403 itself."""
    from .permissions import role_for
    from .utils import audit

    user = getattr(request, "user", None)
    actor = user if getattr(user, "is_authenticated", False) else None
    if actor is None and api_key is None:
        return
    try:
        who = f"key:{api_key.pk}" if api_key is not None else f"user:{actor.pk}"
        if not cache.add(f"denied:{who}:{request.method}:{request.path}", 1, DENIAL_REPEAT_SECONDS):
            return
        if org is not None and api_key is None and role_for(actor, org) is None:
            org = None
        data = {"method": request.method, "path": request.path[:200], "permission": permission, "reason": reason[:300]}
        if api_key is not None:
            data["api_key"] = api_key.name
        audit(org, "auth.denied", request.path[:200], actor=actor, **data)
    except Exception:
        log.exception("Could not record a refused action")


def permission_denied(request, exception=None):
    message = str(exception) if exception and str(exception) else "Your role doesn't allow this action."
    record_denial(request, org=getattr(exception, "org", None) or getattr(request, "org", None),
                  permission=getattr(exception, "perm", ""), reason=message)
    return render(request, "errors/403.html", {"message": message}, status=403)


def not_found(request, exception=None):
    return render(request, "errors/404.html", status=404)


def server_error(request):
    template = loader.get_template("errors/500.html")
    return HttpResponseServerError(template.render({"request_id": getattr(request, "request_id", "")}))


def csrf_failure(request, reason=""):
    """The form's security token was missing or didn't match: almost always a page left open until the session
    ended. Say that in plain words and offer the way back; the technical reason goes to the log only."""
    log.warning("CSRF check failed for %s %s: %s", request.method, request.path, reason)
    referer = request.META.get("HTTP_REFERER", "")
    back = referer if referer and url_has_allowed_host_and_scheme(
        referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()) else "/"
    return render(request, "errors/csrf.html", {"back": back}, status=403)
