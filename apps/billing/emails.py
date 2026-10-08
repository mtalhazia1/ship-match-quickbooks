"""Emails from billing and sign-up: verification links, usage notices and payment problems.

Plain text through Django's email settings (on a public demo the demo backend logs them instead of sending).
"""
from __future__ import annotations

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

from apps.core.models import Membership


def admin_emails(org) -> list[str]:
    rows = (Membership.objects.filter(organization=org, role=Membership.Role.ADMIN, user__is_active=True)
            .exclude(user__email="").values_list("user__email", flat=True))
    return sorted({e.strip().lower() for e in rows if e and "@" in e})[:20]


def _billing_url() -> str:
    return f"{settings.SITE_URL}{reverse('billing:settings')}"


def _send(subject: str, body: str, to: list[str]) -> int:
    if not to:
        return 0
    return send_mail(subject=subject, message=body, from_email=None, recipient_list=to, fail_silently=False)


def usage_email(org, which: str, usage) -> int:
    end = f"{usage.period_end:%d %b %Y}"
    if usage.trial:
        if which == "soft":
            subject = f"{org.name} has used the free trial's documents on ShipMatch"
            body = (f"{org.name} has received {usage.used:,} documents, the {usage.allowance:,} included in the free "
                    f"trial. New documents keep coming in until {usage.hard_limit:,}, then pause.\n\n"
                    f"Choose a plan to keep them coming:\n{_billing_url()}\n")
        else:
            subject = f"New documents are paused for {org.name} on ShipMatch"
            body = (f"{org.name} has received {usage.used:,} documents, the most the free trial includes, so new "
                    "uploads, emails and API uploads are refused.\n\nEverything already received can still be "
                    f"reviewed, approved and posted.\n\nChoose a plan to start intake again:\n{_billing_url()}\n")
        return _send(subject, body, admin_emails(org))
    if which == "soft":
        subject = f"{org.name} has used this month's documents on ShipMatch"
        body = (f"{org.name} has received {usage.used:,} documents this billing month, the {usage.allowance:,} "
                f"included in its plan.\n\nNothing has stopped. New documents keep coming in until "
                f"{usage.hard_limit:,} ({settings.BILLING_HARD_LIMIT_PERCENT}% of the plan); after that uploads and "
                f"emails are paused until {end}.\n\nTo raise the limit, choose a bigger plan:\n{_billing_url()}\n")
    else:
        subject = f"New documents are paused for {org.name} on ShipMatch"
        body = (f"{org.name} has received {usage.used:,} documents this billing month, "
                f"{settings.BILLING_HARD_LIMIT_PERCENT}% of the {usage.allowance:,} in its plan, so new uploads, "
                f"emails and API uploads are refused until {end}.\n\nEverything already received can still be "
                f"reviewed, approved and posted.\n\nChoose a bigger plan to start intake again straight away:\n"
                f"{_billing_url()}\n")
    return _send(subject, body, admin_emails(org))


def payment_failed_email(org, amount: str = "", next_try: str = "") -> int:
    subject = f"Payment for ShipMatch failed ({org.name})"
    body = (f"Stripe couldn't take the payment{f' of {amount}' if amount else ''} for {org.name}'s ShipMatch "
            "subscription.")
    if next_try:
        body += f" It will try again on {next_try}."
    body += ("\n\nShipMatch keeps working while Stripe retries. Update the card or payment method here to avoid "
             f"a pause:\n{_billing_url()}\n")
    return _send(subject, body, admin_emails(org))


def verification_email(to: str, company: str, link: str) -> int:
    hours = settings.SIGNUP_VERIFY_HOURS
    body = (f"Confirm your email address to finish setting up {company} on ShipMatch:\n\n{link}\n\n"
            f"The link works for {hours} hours and only once. If you didn't sign up, ignore this email; "
            "nothing is created until the link is opened.\n")
    return _send("Confirm your email for ShipMatch", body, [to])


def already_registered_email(to: str, login_url: str, reset_url: str) -> int:
    body = ("Someone, probably you, tried to sign up for ShipMatch with this email address. You already have an "
            f"account, so nothing new was created.\n\nSign in: {login_url}\nForgot your password? {reset_url}\n\n"
            "If this wasn't you, you can ignore this email.\n")
    return _send("You already have a ShipMatch account", body, [to])
