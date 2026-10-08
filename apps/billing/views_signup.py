"""Public sign-up pages (SIGNUP_ENABLED=1): the form, "check your email", the verification link and resending it."""
from __future__ import annotations

import logging
from urllib.parse import unquote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from apps.core.context import client_ip_var
from apps.core.middleware import valid_timezone

from . import signup
from .emails import already_registered_email, verification_email

log = logging.getLogger(__name__)


def _require_enabled():
    if not settings.SIGNUP_ENABLED:
        raise Http404("Sign-up is not open on this server.")


def _private(response):
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Cache-Control"] = "no-store, private"
    return response


def _sender(request, form_company: str = ""):
    def send(pending):
        try:
            if pending is None:
                already_registered_email(
                    request.POST.get("email", "").strip().lower(),
                    request.build_absolute_uri(reverse("accounts:login")),
                    request.build_absolute_uri(reverse("accounts:password_reset")))
            else:
                link = request.build_absolute_uri(reverse("signup:verify", args=[signup.make_token(pending)]))
                verification_email(pending.email, pending.company_name or form_company, link)
        except Exception:
            log.exception("Could not send a sign-up email")
    return send


def _context(**extra) -> dict:
    return {"honeypot": signup.HONEYPOT, "trial_days": settings.BILLING_TRIAL_DAYS,
            "billing": settings.BILLING_ENABLED, "verify_hours": settings.SIGNUP_VERIFY_HOURS, **extra}


@sensitive_post_parameters("password")
@never_cache
@require_http_methods(["GET", "POST"])
def signup_page(request):
    _require_enabled()
    if request.user.is_authenticated:
        messages.info(request, "You're signed in already. To add another company, ask us to set it up.")
        return redirect("core:dashboard")
    if request.method == "GET":
        return _private(render(request, "billing/signup.html", _context(form=signup.SignupForm(), errors={})))
    form = signup.SignupForm.from_post(request.POST)
    iph = signup.ip_hash(client_ip_var.get())
    limited = signup.rate_limited(iph)
    if limited:
        return _private(render(request, "billing/signup.html",
                               _context(form=form, errors={}, error=limited), status=429))
    if request.POST.get(signup.HONEYPOT):
        log.info("Sign-up: honeypot field filled, ignored")
        # Looks like success to a bot; nothing is stored or sent.
        return redirect("signup:sent")
    errors = signup.validate(form)
    if errors:
        return _private(render(request, "billing/signup.html", _context(form=form, errors=errors), status=400))
    signup.start(form, iph, _sender(request, form.company_name))
    request.session["signup_email"] = form.email
    return redirect("signup:sent")


@never_cache
def sent(request):
    _require_enabled()
    return _private(render(request, "billing/signup_sent.html",
                           _context(email=request.session.get("signup_email", ""))))


@sensitive_post_parameters("password")
@never_cache
def verify(request, token: str):
    _require_enabled()
    try:
        pending = signup.read_token(token)
    except signup.LinkExpired as e:
        return _private(render(request, "billing/signup_link.html",
                               _context(state="expired", email=e.pending.email if e.pending else ""), status=410))
    except signup.LinkInvalid:
        return _private(render(request, "billing/signup_link.html", _context(state="invalid"), status=404))
    if pending.verified_at is not None:
        return _private(render(request, "billing/signup_link.html", _context(state="used")))
    if request.method != "POST":
        # Mail scanners open links; the account is only created when the person presses the button.
        return _private(render(request, "billing/signup_link.html",
                               _context(state="confirm", pending=pending, token=token)))
    try:
        signup.check_link_password(pending, request.POST.get("password", ""))
    except signup.WrongPassword:
        return _private(render(request, "billing/signup_link.html",
                               _context(state="confirm", pending=pending, token=token,
                                        error="That isn't the password chosen when signing up. If you didn't sign "
                                              "up, ignore this page and the email."), status=400))
    except signup.LinkInvalid:
        return _private(render(request, "billing/signup_link.html", _context(state="invalid"), status=404))
    tz = valid_timezone(unquote(request.COOKIES.get("tz", ""))) or ""
    try:
        user, org = signup.complete(pending, tz)
    except signup.AlreadyRegistered:
        return _private(render(request, "billing/signup_link.html", _context(state="used")))
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session["org"] = org.slug
    request.session.pop("signup_email", None)
    if settings.BILLING_ENABLED:
        messages.success(request, f"Welcome to ShipMatch. {org.name} is ready and your "
                                  f"{settings.BILLING_TRIAL_DAYS}-day free trial has started.")
    else:
        messages.success(request, f"Welcome to ShipMatch. {org.name} is ready.")
    return redirect("core:dashboard")


@require_POST
@never_cache
def resend(request):
    _require_enabled()
    limited = signup.rate_limited(signup.ip_hash(client_ip_var.get()))
    if limited:
        messages.error(request, limited)
        return redirect("signup:sent")
    email = request.POST.get("email", "").strip().lower()
    signup.resend(email, _sender(request))
    request.session["signup_email"] = email
    messages.info(request, "If that address has a sign-up waiting, a new link is on its way.")
    return redirect("signup:sent")
