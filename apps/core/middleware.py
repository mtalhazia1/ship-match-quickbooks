"""Request-wide middleware: request IDs, current organization, time zones, idle timeout,
2FA enforcement and security headers."""
from __future__ import annotations

import re
import time
import uuid
import zoneinfo
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .context import client_ip_var, request_id_var
from .models import Organization
from .utils import orgs_for_user

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_VALID_TZ = None


def valid_timezone(name: str | None) -> str | None:
    global _VALID_TZ
    if not name:
        return None
    if _VALID_TZ is None:
        _VALID_TZ = zoneinfo.available_timezones()
    return name if name in _VALID_TZ else None


def client_ip(request) -> str | None:
    if settings.TRUST_X_FORWARDED_FOR:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


class RequestContextMiddleware:
    """Assigns every request an ID (or reuses a sane X-Request-ID) for logs, audit rows and support."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get("HTTP_X_REQUEST_ID", "")
        request.request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        rid_token = request_id_var.set(request.request_id)
        ip_token = client_ip_var.set(client_ip(request))
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(rid_token)
            client_ip_var.reset(ip_token)
        response["X-Request-ID"] = request.request_id
        return response


class CurrentOrganizationMiddleware:
    """Sets request.org: the organization chosen with ?org=<slug>, else the last one used, else the first."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.org = None
        if request.user.is_authenticated:
            orgs = orgs_for_user(request.user)
            slug = request.GET.get("org") or request.session.get("org")
            org = orgs.filter(slug=slug).first() if slug else None
            org = org or orgs.order_by("name").first()
            if org:
                request.org = org
                if request.session.get("org") != org.slug:
                    request.session["org"] = org.slug
        return self.get_response(request)


class TimezoneMiddleware:
    """Show times in the user's zone: profile setting, then the browser (tz cookie), then the organization."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tz, source = None, "default"
        if request.user.is_authenticated:
            profile = getattr(request.user, "profile", None)
            tz = valid_timezone(getattr(profile, "timezone", ""))
            source = "profile" if tz else source
        if not tz:
            tz = valid_timezone(unquote(request.COOKIES.get("tz", "")))
            source = "browser" if tz else source
        org: Organization | None = getattr(request, "org", None)
        if not tz and org:
            tz = valid_timezone(org.timezone)
            source = "organization" if tz else source
        if tz:
            timezone.activate(zoneinfo.ZoneInfo(tz))
        else:
            timezone.deactivate()
        request.timezone_name = tz or settings.TIME_ZONE
        request.timezone_source = source
        return self.get_response(request)


class IdleTimeoutMiddleware:
    """Signs a user out after SESSION_IDLE_TIMEOUT seconds without a request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and settings.SESSION_IDLE_TIMEOUT:
            now = int(time.time())
            last = request.session.get("last_activity")
            if last and now - last > settings.SESSION_IDLE_TIMEOUT:
                logout(request)
                messages.info(request, "You were signed out after a period of inactivity. Sign in again to continue.")
                return redirect(f"{reverse('accounts:login')}?next={request.path}")
            if not last or now - last > 30:  # avoid a session write on every request
                request.session["last_activity"] = now
        return self.get_response(request)


class MFAEnforcementMiddleware:
    """If the organization requires 2FA, members without it can only reach the 2FA setup page."""

    ALLOWED_PREFIXES = ("/account/", "/static/", "/health/", "/media/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        org = getattr(request, "org", None)
        if (request.user.is_authenticated and org is not None and org.require_mfa
                and not request.path.startswith(self.ALLOWED_PREFIXES)):
            profile = getattr(request.user, "profile", None)
            if not (profile and profile.mfa_enabled):
                messages.warning(request, f"{org.name} requires two-factor authentication. Set it up to continue.")
                return redirect("accounts:security")
        return self.get_response(request)


class SecurityHeadersMiddleware:
    """Content Security Policy and related headers for HTML pages.

    Scripts may only come from this site (no inline scripts). The Django admin and the API
    docs page are excluded because they ship their own inline code.
    """

    EXEMPT_PREFIXES = ("/admin/", "/api/docs")

    def __init__(self, get_response):
        self.get_response = get_response
        self.policy = "; ".join([
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            "frame-src 'self'",
            "frame-ancestors 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        ])

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        if (response.get("Content-Type", "").startswith("text/html")
                and not request.path.startswith(self.EXEMPT_PREFIXES)):
            response.setdefault("Content-Security-Policy", self.policy)
        return response


class LoginNextMiddleware:
    """After a signed-out person submits a form, send them back to the page they were on, not to the form's URL.

    Django (and the idle timeout) remember the address that was requested as the place to return to after
    sign-in. For a form submission that address only accepts POST, so signing in again ended on an empty
    "405 Method Not Allowed" page. The page the form was on (the Referer, same site only) is what the person
    wants back; without one they simply go to the dashboard."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.method != "POST" or response.status_code != 302:
            return response
        login_path = reverse(settings.LOGIN_URL)
        target = urlsplit(response["Location"])
        if target.path != login_path or parse_qs(target.query).get("next", [""])[0] not in (
                request.path, request.get_full_path()):
            return response
        referer = request.META.get("HTTP_REFERER", "")
        back = urlsplit(referer)
        usable = (referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()},
                                                              require_https=request.is_secure())
                  and back.path.startswith("/") and back.path != request.path)
        if usable:
            nxt = back.path + ("?" + back.query if back.query else "")
            response["Location"] = f"{login_path}?{urlencode({'next': nxt}, safe='/')}"
        else:
            response["Location"] = login_path
        return response
