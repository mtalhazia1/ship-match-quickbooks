"""Branded error pages. The 500 page renders without the database or request context."""
import logging

from django.http import HttpResponseServerError
from django.shortcuts import render
from django.template import loader
from django.utils.http import url_has_allowed_host_and_scheme

log = logging.getLogger(__name__)


def permission_denied(request, exception=None):
    message = str(exception) if exception and str(exception) else "Your role doesn't allow this action."
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
