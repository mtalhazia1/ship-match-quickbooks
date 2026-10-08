"""DEMO_MODE guard rails: the shared demo accounts can try everything except what would break the
demo for the next visitor (their passwords and two-factor, removing people, QuickBooks, security settings).

Placed after the authentication and message middleware and before CurrentOrganizationMiddleware.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.encoding import force_str
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode

from apps.core.models import Membership

from .services.accounts import demo_emails, demo_usernames, is_demo_user

PREFIX = "This is a shared demo, so "
SUFFIX = " Everything else works as it does in your own ShipMatch."


def _target_is_demo(kwargs) -> bool:
    m = Membership.objects.filter(pk=kwargs.get("pk")).select_related("user").first()
    return bool(m) and m.user.get_username() in demo_usernames()


def _security_change(request) -> bool:
    org = getattr(request, "org", None)
    if org is None:
        return False
    return ((request.POST.get("require_mfa") == "on") != org.require_mfa
            or (request.POST.get("maker_checker") == "on") != org.maker_checker)


def _reset_target_is_demo(kwargs) -> bool:
    try:
        uid = force_str(urlsafe_base64_decode(kwargs.get("uidb64", "")))
        user = get_user_model().objects.filter(pk=uid).first()
    except (TypeError, ValueError, OverflowError):
        return False
    return bool(user) and user.get_username() in demo_usernames()


# url name -> (who it applies to, condition(request, kwargs), message, where to go back to)
#   who: "demo" = only when a demo account is signed in, "anyone" = also anonymous visitors
MFA = "two-factor settings of the demo accounts can't be changed."
RULES = {
    "accounts:password_change": ("demo", None, "the demo accounts' passwords can't be changed.", "accounts:security"),
    "accounts:mfa_start": ("demo", None, MFA, "accounts:security"),
    "accounts:mfa_confirm": ("demo", None, MFA, "accounts:security"),
    "accounts:mfa_disable": ("demo", None, MFA, "accounts:security"),
    "accounts:mfa_recovery_codes": ("demo", None, MFA, "accounts:security"),
    "accounts:password_reset": ("anyone", lambda r, kw: r.POST.get("email", "").strip().lower() in demo_emails(),
                                "passwords of the demo accounts can't be reset.", "accounts:login"),
    "accounts:password_reset_confirm": ("anyone", lambda r, kw: _reset_target_is_demo(kw),
                                        "passwords of the demo accounts can't be changed.", "accounts:login"),
    "core:remove_member": ("demo", None, "removing team members is turned off.", "core:team"),
    "core:update_member": ("demo", lambda r, kw: _target_is_demo(kw), "the demo accounts keep their roles and "
                           "limits. Invite a new member to try changing a role.", "core:team"),
    "core:reset_member_mfa": ("demo", lambda r, kw: _target_is_demo(kw), MFA, "core:team"),
    "core:password_link": ("demo", lambda r, kw: _target_is_demo(kw), "passwords of the demo accounts can't be "
                           "changed.", "core:team"),
    "core:invite": ("demo", lambda r, kw: bool(settings.EMAIL_HOST), "invitations are turned off because they "
                    "would send real email.", "core:team"),
    "accounting:disconnect": ("demo", None, "QuickBooks can't be disconnected.", "core:settings"),
    "core:settings": ("demo", lambda r, kw: _security_change(r), "maker-checker and required two-factor "
                      "authentication can't be changed. Nothing was saved; change the other settings on their own.",
                      "core:settings"),
}


class DemoModeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.DEMO_MODE and is_demo_user(request.user):
            user = request.user
            if user.is_superuser or user.is_staff:
                # Demo accounts never get platform-admin powers on a public demo (in memory, never saved).
                user.is_superuser = user.is_staff = False
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not settings.DEMO_MODE:
            return None
        demo = is_demo_user(request.user)
        if demo and request.path.startswith("/admin/"):
            messages.info(request, PREFIX + "the platform admin site is not available." + SUFFIX)
            return redirect("core:dashboard")
        match = getattr(request, "resolver_match", None)
        if request.method != "POST" or match is None:
            return None
        rule = RULES.get(f"{match.namespace}:{match.url_name}")
        if rule is None:
            return None
        who, condition, message, back = rule
        if who == "demo" and not demo:
            return None
        if condition is not None and not condition(request, view_kwargs):
            return None
        messages.warning(request, PREFIX + message + SUFFIX)
        referer = request.META.get("HTTP_REFERER", "")
        if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()},
                                                       require_https=request.is_secure()):
            return redirect(referer)
        return redirect(reverse(back))
