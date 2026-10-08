"""Self-serve sign-up: checks, rate limits, the emailed verification link, and creating the organization.

Nothing is created until the email address is verified: the sign-up waits as a PendingSignup (with the
password already hashed). Opening the link creates the user, the organization (with a free slug), the admin
membership, the free trial and the onboarding checklist, in one transaction.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.core.models import Membership, Organization
from apps.core.utils import audit

from .models import BillingAccount, Onboarding, PendingSignup

log = logging.getLogger(__name__)

SALT = "shipmatch.signup.verify"
HONEYPOT = "website"
MAX_EMAILS_PER_ADDRESS_PER_HOUR = 3

# Throwaway inbox services. A short list on purpose: it stops casual abuse, not a determined person.
# SIGNUP_BLOCK_DISPOSABLE=0 turns the check off; SIGNUP_BLOCKED_DOMAINS adds more.
DISPOSABLE_DOMAINS = frozenset({
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "guerrillamail.org", "guerrillamail.biz",
    "sharklasers.com", "grr.la", "10minutemail.com", "10minutemail.net", "tempmail.com", "temp-mail.org",
    "temp-mail.io", "tempmail.dev", "tempmailo.com", "yopmail.com", "yopmail.net", "trashmail.com",
    "trashmail.de", "getnada.com", "nada.email", "dispostable.com", "maildrop.cc", "throwawaymail.com",
    "fakeinbox.com", "mintemail.com", "mohmal.com", "emailondeck.com", "burnermail.io", "spamgourmet.com",
    "mailnesia.com", "mytemp.email", "tempr.email", "discard.email", "mailcatch.com", "inboxkitten.com",
    "moakt.com", "minuteinbox.com", "tmpmail.org", "tmpmail.net", "spambox.us", "getairmail.com",
})

RESERVED_SLUGS = frozenset({
    "admin", "api", "app", "www", "static", "media", "demo", "try", "signup", "login", "account", "billing",
    "settings", "help", "support", "root", "system", "shipmatch", "pilot", "test", "null", "none",
})


class SignupError(ValueError):
    """Shown to the visitor; field = which input it belongs to ('' = the whole form)."""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


@dataclass
class SignupForm:
    company_name: str = ""
    full_name: str = ""
    email: str = ""
    password: str = ""

    @classmethod
    def from_post(cls, post) -> SignupForm:
        clean = lambda s: re.sub(r"[\x00-\x1f\x7f]+", " ", s or "").strip()  # noqa: E731
        return cls(company_name=clean(post.get("company_name"))[:200], full_name=clean(post.get("full_name"))[:150],
                   email=(post.get("email") or "").strip().lower()[:254], password=post.get("password") or "")


# --------------------------------------------------------------------------- abuse controls


def _ip_bucket(ip: str | None) -> str:
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return ip or "unknown"
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return str(mapped)
    if addr.version == 6:  # one home or server gets a whole /64
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def ip_hash(ip: str | None) -> str:
    return hashlib.sha256(f"{settings.SECRET_KEY}|signup|{_ip_bucket(ip)}".encode()).hexdigest()[:32]


def _incr(key: str, ttl: int) -> int:
    cache.add(key, 0, ttl)
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, ttl)
        return 1


def rate_limited(iph: str) -> str:
    """'' if this visitor may try now, else what to tell them. Counts the attempt."""
    now = timezone.now()
    hour_key, day_key = f"signup:ip:{iph}:h:{now:%Y%m%d%H}", f"signup:ip:{iph}:d:{now:%Y%m%d}"
    if (cache.get(hour_key) or 0) >= settings.SIGNUP_RATE_PER_HOUR:
        return "Too many sign-up attempts from your network in the last hour. Try again in an hour."
    if (cache.get(day_key) or 0) >= settings.SIGNUP_RATE_PER_DAY:
        return "Too many sign-up attempts from your network today. Try again tomorrow."
    _incr(hour_key, 3600 + 60)
    _incr(day_key, 86400 + 60)
    return ""


def _email_quota_left(email: str) -> bool:
    """At most a few verification emails per address per hour, so nobody can flood someone else's inbox."""
    key = f"signup:mail:{hashlib.sha256(email.encode()).hexdigest()[:24]}:{timezone.now():%Y%m%d%H}"
    return _incr(key, 3600 + 60) <= MAX_EMAILS_PER_ADDRESS_PER_HOUR


def domain_blocked(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower().rstrip(".")
    blocked = set(settings.SIGNUP_BLOCKED_DOMAINS)
    if settings.SIGNUP_BLOCK_DISPOSABLE:
        blocked |= DISPOSABLE_DOMAINS
    return any(domain == d or domain.endswith("." + d) for d in blocked)


# --------------------------------------------------------------------------- checks


def validate(form: SignupForm) -> dict[str, str]:
    """Field -> error message (empty dict = fine)."""
    errors: dict[str, str] = {}
    if len(form.company_name) < 2:
        errors["company_name"] = "Enter your company's name."
    if len(form.full_name) < 2:
        errors["full_name"] = "Enter your name."
    try:
        validate_email(form.email)
    except ValidationError:
        errors["email"] = "Enter a valid email address, for example ap@yourcompany.com."
    else:
        if domain_blocked(form.email):
            errors["email"] = ("Throwaway email addresses can't be used. Sign up with your work email so we can "
                               "reach you about your account.")
    if not form.password:
        errors["password"] = "Choose a password."
    else:
        first, _, last = form.full_name.partition(" ")
        probe = get_user_model()(username=form.email, email=form.email, first_name=first[:150], last_name=last[:150])
        try:
            validate_password(form.password, user=probe)
        except ValidationError as e:
            errors["password"] = " ".join(e.messages)
    return errors


# --------------------------------------------------------------------------- link


def make_token(pending: PendingSignup) -> str:
    return signing.dumps({"p": pending.pk, "n": pending.nonce}, salt=SALT)


class LinkExpired(Exception):
    def __init__(self, pending: PendingSignup | None):
        self.pending = pending


class LinkInvalid(Exception):
    pass


def read_token(token: str) -> PendingSignup:
    max_age = settings.SIGNUP_VERIFY_HOURS * 3600
    try:
        data = signing.loads(token, salt=SALT, max_age=max_age)
    except signing.SignatureExpired:
        try:
            data = signing.loads(token, salt=SALT)
        except signing.BadSignature:
            raise LinkInvalid()
        raise LinkExpired(_pending_for(data))
    except signing.BadSignature:
        raise LinkInvalid()
    pending = _pending_for(data)
    if pending is None:
        raise LinkInvalid()
    return pending


def _pending_for(data) -> PendingSignup | None:
    if not isinstance(data, dict):
        return None
    pending = PendingSignup.objects.filter(pk=data.get("p")).first()
    if pending is None or not secrets.compare_digest(pending.nonce, str(data.get("n") or "")):
        return None
    return pending


# --------------------------------------------------------------------------- flow


def user_exists(email: str) -> bool:
    User = get_user_model()
    return User.objects.filter(email__iexact=email).exists() or User.objects.filter(username__iexact=email).exists()


def start(form: SignupForm, iph: str, send_link) -> PendingSignup | None:
    """Store the sign-up and email the link. send_link(pending) sends it (the view knows the site address).

    An address that already has an account gets a "you already have an account" email instead; the visitor sees
    the same "check your email" page either way, so nobody can find out which addresses are registered.
    """
    if not _email_quota_left(form.email):
        log.info("Sign-up: verification email quota reached for an address")
        return None
    if user_exists(form.email):
        make_password(form.password)   # same work as a new sign-up, so timing doesn't reveal registered addresses
        send_link(None)
        return None
    pending = PendingSignup.objects.create(
        company_name=form.company_name, full_name=form.full_name, email=form.email,
        password_hash=make_password(form.password), nonce=secrets.token_hex(16), ip_hash=iph, emails_sent=1)
    send_link(pending)
    return pending


def resend(email: str, send_link) -> None:
    email = (email or "").strip().lower()
    pending = PendingSignup.objects.filter(email=email, verified_at__isnull=True).first()
    if pending is None or not _email_quota_left(email):
        return
    pending.emails_sent += 1
    pending.save(update_fields=["emails_sent"])
    send_link(pending)


def unique_slug(name: str) -> str:
    base = slugify(name)[:40].strip("-") or "company"
    if base in RESERVED_SLUGS or base in {s.lower() for s in settings.DEMO_ORGS}:
        base = f"{base}-co"
    taken = set(Organization.objects.filter(slug__startswith=base).values_list("slug", flat=True))
    if base not in taken:
        return base
    for n in range(2, 100):
        candidate = f"{base}-{n}"
        if candidate not in taken:
            return candidate
    return f"{base}-{secrets.token_hex(3)}"


class WrongPassword(Exception):
    """The password typed on the confirmation page isn't the one chosen at sign-up."""


MAX_CONFIRM_ATTEMPTS = 5


def check_link_password(pending: PendingSignup, raw: str) -> None:
    """Whoever opens the link must know the password chosen at sign-up. Otherwise someone could start a sign-up
    with another person's address and a password of their own, and that person, by confirming, would create an
    account the starter can sign in to. After a few wrong tries the link stops working."""
    from django.contrib.auth.hashers import check_password

    key = f"signup:confirm-tries:{pending.pk}"
    tries = _incr(key, settings.SIGNUP_VERIFY_HOURS * 3600)
    if tries > MAX_CONFIRM_ATTEMPTS:
        raise LinkInvalid()
    if not raw or not check_password(raw, pending.password_hash):
        raise WrongPassword()


class AlreadyRegistered(Exception):
    pass


def complete(pending: PendingSignup, timezone_name: str = "") -> tuple:
    """Create the user, organization, admin membership, trial and checklist. Returns (user, org)."""
    User = get_user_model()
    first, _, last = pending.full_name.partition(" ")
    with transaction.atomic():
        locked = PendingSignup.objects.select_for_update().get(pk=pending.pk)
        if locked.verified_at is not None:
            raise AlreadyRegistered()
        if user_exists(locked.email):
            raise AlreadyRegistered()
        user = User(username=locked.email, email=locked.email, first_name=first[:150], last_name=last[:150])
        user.password = locked.password_hash
        try:
            with transaction.atomic():
                user.save()
        except IntegrityError:
            raise AlreadyRegistered()
        org = None
        for _ in range(5):
            try:
                with transaction.atomic():
                    org = Organization.objects.create(name=locked.company_name, slug=unique_slug(locked.company_name),
                                                      timezone=timezone_name or "UTC")
                break
            except IntegrityError:  # someone took the slug a moment ago
                continue
        if org is None:
            org = Organization.objects.create(name=locked.company_name,
                                              slug=f"{slugify(locked.company_name)[:30] or 'company'}-"
                                                   f"{secrets.token_hex(4)}", timezone=timezone_name or "UTC")
        Membership.objects.create(user=user, organization=org, role=Membership.Role.ADMIN)
        BillingAccount.objects.create(organization=org, status=BillingAccount.Status.TRIALING,
                                      plan=settings.BILLING_TRIAL_PLAN,
                                      trial_ends_at=timezone.now() + timedelta(days=settings.BILLING_TRIAL_DAYS))
        Onboarding.objects.create(organization=org)
        locked.verified_at = timezone.now()
        locked.organization, locked.user = org, user
        locked.password_hash = ""  # the user has it now
        locked.save(update_fields=["verified_at", "organization", "user", "password_hash"])
        audit(org, "signup.completed", org, actor=user, email=locked.email, company=org.name, slug=org.slug)
    return user, org


def purge(older_than_hours: int | None = None) -> int:
    """Delete unverified sign-ups whose link has expired (and their stored password hash)."""
    hours = older_than_hours if older_than_hours is not None else settings.SIGNUP_VERIFY_HOURS + 24
    cutoff = timezone.now() - timedelta(hours=hours)
    n, _ = PendingSignup.objects.filter(verified_at__isnull=True, created_at__lt=cutoff).delete()
    return n
