"""Sign-in (with lockout and two-factor), sign-out, and the account security page."""
from __future__ import annotations

import time
import zoneinfo

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from apps.core.context import client_ip_var
from apps.core import timezones
from apps.core.models import Organization
from apps.core.utils import audit

from .services import lockout, mfa

MFA_PENDING_SECONDS = 300
MFA_MAX_ATTEMPTS = 5


def _safe_next(request, default: str) -> str:
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return nxt
    return default


@sensitive_post_parameters("password")
@csrf_protect
@never_cache
def login_view(request):
    if request.user.is_authenticated:
        return redirect(_safe_next(request, reverse(settings.LOGIN_REDIRECT_URL)))
    form = AuthenticationForm(request, data=request.POST or None)
    ip = client_ip_var.get()
    if request.method == "POST":
        username = request.POST.get("username", "")
        if lockout.is_locked(username, ip):
            audit(None, "auth.locked_out", username[:64], username=username[:64])
            messages.error(request, f"Too many failed sign-in attempts. Try again in "
                                    f"{settings.LOGIN_LOCKOUT_SECONDS // 60} minutes, or ask an admin to reset your password.")
            return render(request, "account/login.html", {"form": AuthenticationForm(request), "next": _safe_next(request, "")})
        if form.is_valid():
            user = form.get_user()
            lockout.reset(username, ip)
            next_url = _safe_next(request, reverse(settings.LOGIN_REDIRECT_URL))
            if mfa.profile_for(user).mfa_enabled:
                request.session["mfa_pending"] = {"uid": user.pk, "backend": user.backend, "ts": int(time.time()),
                                                  "next": next_url, "attempts": 0}
                return redirect("accounts:verify")
            login(request, user)
            return redirect(next_url)
        failures = lockout.register_failure(username, ip)
        audit(None, "auth.login_failed", username[:64], username=username[:64], failures=failures)
    return render(request, "account/login.html", {"form": form, "next": _safe_next(request, "")})


@csrf_protect
@never_cache
def verify_view(request):
    pending = request.session.get("mfa_pending")
    if not pending or time.time() - pending["ts"] > MFA_PENDING_SECONDS:
        request.session.pop("mfa_pending", None)
        messages.info(request, "Your sign-in timed out. Enter your password again.")
        return redirect("accounts:login")
    user = get_user_model().objects.filter(pk=pending["uid"], is_active=True).first()
    if user is None:
        request.session.pop("mfa_pending", None)
        return redirect("accounts:login")
    error = ""
    if request.method == "POST":
        code = request.POST.get("code", "").strip()
        method = "totp" if mfa.verify_totp(user, code) else ("recovery" if mfa.use_recovery_code(user, code) else "")
        if method:
            request.session.pop("mfa_pending", None)
            login(request, user, backend=pending["backend"])
            if method == "recovery":
                left = len(mfa.profile_for(user).recovery_codes)
                audit(None, "auth.recovery_code_used", user, actor=user, remaining=left)
                messages.warning(request, f"You signed in with a recovery code. {left} codes left. "
                                          "Create new ones on your Security page if you are running low.")
            return redirect(pending["next"])
        pending["attempts"] += 1
        audit(None, "auth.mfa_failed", user, username=user.get_username(), attempts=pending["attempts"])
        if pending["attempts"] >= MFA_MAX_ATTEMPTS:
            request.session.pop("mfa_pending", None)
            lockout.register_failure(user.get_username(), client_ip_var.get())
            messages.error(request, "Too many incorrect codes. Sign in again.")
            return redirect("accounts:login")
        request.session["mfa_pending"] = pending
        error = "That code didn't work. Enter the current 6-digit code from your app, or a recovery code."
    return render(request, "account/verify.html", {"error": error, "username": user.get_username()})


@require_POST
def logout_view(request):
    logout(request)
    messages.success(request, "You signed out.")
    return redirect("accounts:login")


def _org_requires_mfa(user) -> list[Organization]:
    return list(Organization.objects.filter(memberships__user=user, require_mfa=True))


@login_required
def security_view(request):
    profile = mfa.profile_for(request.user)
    pending_qr = None
    if profile.mfa_secret and not profile.mfa_enabled:
        pending_qr = mfa.qr_data_uri(request.user, profile.mfa_secret)
    zones = timezones.choices(profile.timezone)
    return render(request, "account/security.html", {
        "profile": profile, "pending_qr": pending_qr, "pending_secret": profile.mfa_secret if pending_qr else "",
        "required_by": _org_requires_mfa(request.user), "zones": zones,
        "recovery_codes": request.session.pop("new_recovery_codes", None),
    })


@login_required
@require_POST
def mfa_start(request):
    mfa.start_enrollment(request.user)
    return redirect("accounts:security")


@login_required
@require_POST
def mfa_confirm(request):
    codes = mfa.confirm_enrollment(request.user, request.POST.get("code", ""))
    if codes is None:
        messages.error(request, "That code didn't match. Check the time on your phone and enter the current code.")
    else:
        audit(None, "auth.mfa_enabled", request.user, actor=request.user)
        request.session["new_recovery_codes"] = codes
        messages.success(request, "Two-factor authentication is on.")
    return redirect("accounts:security")


@login_required
@require_POST
def mfa_disable(request):
    if _org_requires_mfa(request.user):
        messages.error(request, "Your organization requires two-factor authentication, so it can't be turned off.")
    elif not authenticate(request, username=request.user.get_username(), password=request.POST.get("password", "")):
        messages.error(request, "Password is incorrect. Two-factor authentication is still on.")
    else:
        mfa.disable(request.user)
        audit(None, "auth.mfa_disabled", request.user, actor=request.user)
        messages.success(request, "Two-factor authentication is off.")
    return redirect("accounts:security")


@login_required
@require_POST
def mfa_recovery_codes(request):
    if not authenticate(request, username=request.user.get_username(), password=request.POST.get("password", "")):
        messages.error(request, "Password is incorrect.")
    else:
        request.session["new_recovery_codes"] = mfa.new_recovery_codes(request.user)
        audit(None, "auth.recovery_codes_created", request.user, actor=request.user)
        messages.success(request, "New recovery codes created. The old ones no longer work.")
    return redirect("accounts:security")


@login_required
@require_POST
def preferences(request):
    profile = mfa.profile_for(request.user)
    tz = request.POST.get("timezone", "")
    profile.timezone = tz if tz in zoneinfo.available_timezones() else ""
    profile.save(update_fields=["timezone"])
    messages.success(request, "Time zone saved.")
    return redirect("accounts:security")


class PasswordChangeView(auth_views.PasswordChangeView):
    template_name = "account/password_change.html"
    success_url = reverse_lazy("accounts:security")

    def form_valid(self, form):
        if form.user.check_password(form.cleaned_data["new_password1"]):   # checked before the password is saved
            form.add_error("new_password1", "That is your current password. Choose a different one.")
            return self.form_invalid(form)
        response = super().form_valid(form)
        update_session_auth_hash(self.request, form.user)
        audit(None, "auth.password_changed", self.request.user, actor=self.request.user)
        messages.success(self.request, "Password changed.")
        return response


class PasswordResetView(auth_views.PasswordResetView):
    template_name = "account/password_reset.html"
    email_template_name = "account/email/password_reset.txt"
    subject_template_name = "account/email/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "account/password_set.html"
    success_url = reverse_lazy("accounts:password_reset_complete")

    def form_valid(self, form):
        response = super().form_valid(form)
        audit(None, "auth.password_set", form.user, actor=form.user)
        return response
