"""Firms create client organizations themselves (FIRM_CAN_CREATE_ORGS).

The same steps as the seed_demo command and the platform admin: an Organization with a unique slug and an
admin Membership for the person creating it. Controls (two-factor requirement, maker-checker, exchange
rates) can be copied from an organization the creator administers.
"""
from __future__ import annotations

import zoneinfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from apps.core.models import Membership, Organization
from apps.core.utils import audit


class OrgError(ValueError):
    pass


def firm_admin_orgs(user):
    return Organization.objects.filter(memberships__user=user, memberships__role=Membership.Role.ADMIN).distinct()


def can_create(user) -> bool:
    """Only when the installation allows it, and only for people who administer at least one organization."""
    if not getattr(settings, "FIRM_CAN_CREATE_ORGS", False):
        return False
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    return firm_admin_orgs(user).exists()


def unique_slug(name: str) -> str:
    base = slugify(name)[:40].strip("-") or "client"
    slug, n = base, 2
    while Organization.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"[:50]
        n += 1
    return slug


def _billing_on() -> bool:
    from django.apps import apps as django_apps
    from django.conf import settings

    return django_apps.is_installed("apps.billing") and bool(getattr(settings, "BILLING_ENABLED", False))


def _start_trial(org: Organization) -> None:
    """A new client organization starts on the free trial with its own limits (never unlimited)."""
    from datetime import timedelta

    from django.conf import settings
    from django.utils import timezone

    from apps.billing.models import BillingAccount

    BillingAccount.objects.create(organization=org, status=BillingAccount.Status.TRIALING,
                                  plan=settings.BILLING_TRIAL_PLAN,
                                  trial_ends_at=timezone.now() + timedelta(days=settings.BILLING_TRIAL_DAYS))


def create(user, name: str, *, home_currency: str = "USD", tz: str = "UTC", copy_from: Organization | None = None
           ) -> Organization:
    if not can_create(user):
        raise OrgError("Creating organizations isn't turned on for your account.")
    name = (name or "").strip()
    if not name:
        raise OrgError("Give the client organization a name.")
    if len(name) > 200:
        raise OrgError("Keep the name under 200 characters.")
    currency = (home_currency or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise OrgError("Home currency must be a 3-letter code such as USD.")
    if tz not in zoneinfo.available_timezones():
        raise OrgError("Choose a valid time zone.")
    if copy_from is not None and not firm_admin_orgs(user).filter(pk=copy_from.pk).exists():
        raise OrgError("You can only copy settings from an organization you administer.")
    billing = _billing_on()
    if billing and not user.is_superuser:
        from apps.billing.models import BillingAccount

        # Each client organization is billed on its own. Only firms with a paid plan add clients, so a free
        # trial can't be multiplied into many.
        if not BillingAccount.objects.filter(organization__in=firm_admin_orgs(user),
                                             status=BillingAccount.Status.ACTIVE).exists():
            raise OrgError("Choose a paid plan for your own organization (Settings, Billing) before adding client "
                           "organizations.")
    for attempt in range(3):
        try:
            with transaction.atomic():
                org = Organization.objects.create(name=name, slug=unique_slug(name), home_currency=currency,
                                                  timezone=tz)
                if copy_from is not None:
                    org.require_mfa, org.maker_checker = copy_from.require_mfa, copy_from.maker_checker
                    org.review_threshold, org.fx_rates = copy_from.review_threshold, dict(copy_from.fx_rates or {})
                    org.save()
                Membership.objects.create(user=user, organization=org, role=Membership.Role.ADMIN)
                if billing:
                    _start_trial(org)
                audit(org, "org.created", org, actor=user, name=org.name, slug=org.slug,
                      copied_from=copy_from.slug if copy_from else "")
                return org
        except IntegrityError:  # two people chose the same name at the same moment: try the next slug
            if attempt == 2:
                raise OrgError("Couldn't create the organization. Try again.")
    raise OrgError("Couldn't create the organization. Try again.")
