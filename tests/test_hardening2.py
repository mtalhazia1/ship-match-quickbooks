"""Regression tests for the security review of the wave 2 features."""
import io
import zipfile
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from apps.accounting.models import XeroConnection
from apps.billing import signup
from apps.billing.models import BillingAccount, PendingSignup
from apps.billing.usage import UsageLimitReached, intake_block_reason
from apps.core.models import Membership, Organization
from apps.documents.models import Document
from apps.documents.services.ingest import ingest_bytes
from apps.shipments.models import Shipment
from apps.workflow.models import Comment

from .conftest import PASSWORD


def _admin(org, username):
    u = get_user_model().objects.create_user(username, email=f"{username}@example.com", password=PASSWORD)
    Membership.objects.create(user=u, organization=org, role=Membership.Role.ADMIN)
    return u


# ------------------------------------------------------------------ firm-created client organizations


@pytest.mark.django_db
def test_trial_firms_cannot_add_unbilled_client_organizations(client, settings, org):
    settings.BILLING_ENABLED, settings.FIRM_CAN_CREATE_ORGS = True, True
    admin = _admin(org, "trialadmin")
    BillingAccount.objects.create(organization=org, status=BillingAccount.Status.TRIALING,
                                  plan=settings.BILLING_TRIAL_PLAN, trial_ends_at=timezone.now() - timedelta(days=1))
    client.force_login(admin)
    client.post(reverse("workflow:create_org"), {"name": "Freebie", "home_currency": "USD", "timezone": "UTC"})
    assert not Organization.objects.filter(name="Freebie").exists()


@pytest.mark.django_db
def test_paying_firms_add_clients_that_start_on_their_own_trial(client, settings, org):
    settings.BILLING_ENABLED, settings.FIRM_CAN_CREATE_ORGS = True, True
    admin = _admin(org, "payingadmin")
    BillingAccount.objects.create(organization=org, status=BillingAccount.Status.ACTIVE, plan=settings.BILLING_TRIAL_PLAN)
    client.force_login(admin)
    client.post(reverse("workflow:create_org"), {"name": "Client Co", "home_currency": "USD", "timezone": "UTC"})
    new = Organization.objects.get(name="Client Co")
    account = BillingAccount.objects.get(organization=new)
    assert account.status == BillingAccount.Status.TRIALING and account.trial_ends_at > timezone.now()


# ------------------------------------------------------------------ two-factor across organizations


@pytest.mark.django_db
def test_objects_in_an_organization_that_requires_two_factor_are_out_of_reach_without_it(client, org):
    strict = Organization.objects.create(name="Strict Co", slug="strict", require_mfa=True)
    u = _admin(org, "multi")
    Membership.objects.create(user=u, organization=strict, role=Membership.Role.ADMIN)
    s = Shipment.objects.create(organization=strict, status=Shipment.Status.NEEDS_REVIEW)
    XeroConnection.objects.create(organization=strict, tenant_id="t-1", access_expires_at=timezone.now())
    client.force_login(u)
    client.get(reverse("core:dashboard") + "?org=test")
    r = client.post(reverse("workflow:comment_create"), {"target": f"shipment:{s.pk}", "body": "no 2FA here"})
    assert r.status_code == 403 and not Comment.objects.filter(body="no 2FA here").exists()
    client.get(reverse("core:dashboard") + "?org=test")
    r = client.post(reverse("accounting:xero_disconnect", args=[strict.pk]))
    assert r.status_code == 403 and XeroConnection.objects.filter(organization=strict).exists()


# ------------------------------------------------------------------ ZIPs and the plan limit


@pytest.mark.django_db
def test_a_zip_cannot_carry_more_documents_than_the_plan_has_room_for(settings, org, dataset):
    settings.BILLING_ENABLED = True
    settings.BILLING_TRIAL_DOCUMENTS = 3
    BillingAccount.objects.create(organization=org, status=BillingAccount.Status.TRIALING,
                                  plan=settings.BILLING_TRIAL_PLAN, trial_ends_at=timezone.now() + timedelta(days=5))
    assert intake_block_reason(org) == ""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in sorted((dataset / "pdf").glob("*.pdf"))[:8]:
            z.writestr(f.name, f.read_bytes())
    with pytest.raises(UsageLimitReached, match="holds 8 documents"):
        ingest_bytes(org, "batch.zip", buf.getvalue(), process="none")
    assert not Document.objects.filter(organization=org).exists()


# ------------------------------------------------------------------ sign-up


@pytest.mark.django_db
def test_signup_link_only_completes_with_the_password_chosen(client, settings):
    settings.SIGNUP_ENABLED = True
    form = signup.SignupForm(company_name="Victim Imports", full_name="V", email="victim@example.com",
                             password="Attacker-chosen-9!")
    pending = signup.start(form, "iph", lambda p: None)
    url = reverse("signup:verify", args=[signup.make_token(pending)])
    r = client.post(url, {"password": "something-else-1!"})
    assert r.status_code == 400 and not get_user_model().objects.filter(email="victim@example.com").exists()
    r = client.post(url, {"password": "Attacker-chosen-9!"})
    assert r.status_code == 302 and get_user_model().objects.filter(email="victim@example.com").exists()


@pytest.mark.django_db
def test_signup_link_stops_working_after_repeated_wrong_passwords(client, settings):
    settings.SIGNUP_ENABLED = True
    form = signup.SignupForm(company_name="Guess Co", full_name="G", email="guess@example.com", password="Right-pass-9!")
    pending = signup.start(form, "iph", lambda p: None)
    url = reverse("signup:verify", args=[signup.make_token(pending)])
    for _ in range(signup.MAX_CONFIRM_ATTEMPTS):
        client.post(url, {"password": "wrong"})
    r = client.post(url, {"password": "Right-pass-9!"})
    assert r.status_code == 404 and PendingSignup.objects.get(pk=pending.pk).verified_at is None


@pytest.mark.django_db
def test_registered_addresses_cost_the_same_password_hashing(monkeypatch, user):
    calls = []
    monkeypatch.setattr(signup, "make_password", lambda raw: calls.append(raw) or "hash")
    form = signup.SignupForm(company_name="X", full_name="Y", email=user.email, password="Another-pass-9!")
    assert signup.start(form, "iph", lambda p: None) is None and calls == ["Another-pass-9!"]
