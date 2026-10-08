"""Self-serve sign-up: verification link, expiry, honeypot, rate limits, slugs, abuse checks, onboarding."""
import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail, signing
from django.urls import reverse
from django.utils import timezone

from apps.billing import signup
from apps.billing.models import BillingAccount, Onboarding, PendingSignup
from apps.core.models import AuditEvent, Membership, Organization

GOOD = {"company_name": "Harbor Freight Imports", "full_name": "Dana Whitfield",
        "email": "dana@harborfreight.example", "password": "copper-kettle-harbor-91"}


@pytest.fixture
def open_signup(settings):
    settings.SIGNUP_ENABLED = True
    settings.BILLING_ENABLED = True
    settings.SIGNUP_RATE_PER_HOUR = 5
    settings.SIGNUP_RATE_PER_DAY = 20
    return settings


def _link(text: str) -> str:
    return re.search(r"https?://\S+/signup/verify/\S+/", text).group(0)


def _path(link: str) -> str:
    return "/" + link.split("://", 1)[1].split("/", 1)[1]


@pytest.mark.django_db
def test_signup_is_off_by_default(client, settings):
    settings.SIGNUP_ENABLED = False
    assert client.get(reverse("signup:start")).status_code == 404
    assert client.post(reverse("signup:start"), GOOD).status_code == 404
    assert "Create an account" not in client.get(reverse("accounts:login")).content.decode()


@pytest.mark.django_db
def test_signup_verification_creates_org_admin_trial_and_checklist(client, open_signup):
    page = client.get(reverse("accounts:login")).content.decode()
    assert reverse("signup:start") in page                          # the sign-in page links to sign-up
    r = client.post(reverse("signup:start"), GOOD)
    assert r.status_code == 302 and r.url == reverse("signup:sent")
    # Nothing is created before the email is verified.
    assert not Organization.objects.filter(name=GOOD["company_name"]).exists()
    assert not get_user_model().objects.filter(email=GOOD["email"]).exists()
    pending = PendingSignup.objects.get(email=GOOD["email"])
    assert pending.password_hash and GOOD["password"] not in pending.password_hash
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [GOOD["email"]]
    path = _path(_link(mail.outbox[0].body))

    # Opening the link (a mail scanner does that too) only shows a confirm button.
    page = client.get(path)
    assert page.status_code == 200 and b"Confirm and open ShipMatch" in page.content
    assert not Organization.objects.filter(name=GOOD["company_name"]).exists()

    r = client.post(path, {"password": GOOD["password"]})
    assert r.status_code == 302 and r.url == reverse("core:dashboard")
    org = Organization.objects.get(name=GOOD["company_name"])
    assert org.slug == "harbor-freight-imports"
    user = get_user_model().objects.get(email=GOOD["email"])
    assert user.username == GOOD["email"] and user.first_name == "Dana" and user.check_password(GOOD["password"])
    assert Membership.objects.get(user=user, organization=org).role == Membership.Role.ADMIN
    account = BillingAccount.objects.get(organization=org)
    assert account.status == BillingAccount.Status.TRIALING
    assert timedelta(days=13, hours=23) < account.trial_ends_at - timezone.now() <= timedelta(days=14)
    assert Onboarding.objects.filter(organization=org).exists()
    assert AuditEvent.objects.filter(organization=org, action="signup.completed").exists()
    pending.refresh_from_db()
    assert pending.verified_at and pending.password_hash == ""

    # Signed in, on the new organization's dashboard, with the getting-started checklist.
    dash = client.get(reverse("core:dashboard"))
    html = dash.content.decode()
    assert dash.status_code == 200 and "Get started" in html and "0 of 4 done" in html
    assert "Connect your accounting system" in html and "Invite your team" in html

    # The link works once.
    again = client.post(path)
    assert again.status_code == 200 and b"Your account is ready" in again.content
    assert Organization.objects.filter(name=GOOD["company_name"]).count() == 1


@pytest.mark.django_db
def test_expired_link_offers_a_new_one(client, open_signup, monkeypatch):
    client.post(reverse("signup:start"), GOOD)
    path = _path(_link(mail.outbox[0].body))
    later = timezone.now().timestamp() + (open_signup.SIGNUP_VERIFY_HOURS + 1) * 3600
    monkeypatch.setattr(signing.time, "time", lambda: later)
    r = client.get(path)
    assert r.status_code == 410 and b"This link has expired" in r.content and GOOD["email"].encode() in r.content
    assert client.post(path).status_code == 410
    assert not Organization.objects.filter(name=GOOD["company_name"]).exists()
    monkeypatch.undo()
    # Resend gives a fresh, working link.
    client.post(reverse("signup:resend"), {"email": GOOD["email"]})
    assert len(mail.outbox) == 2
    assert client.post(_path(_link(mail.outbox[1].body)), {"password": GOOD["password"]}).status_code == 302
    assert Organization.objects.filter(name=GOOD["company_name"]).exists()


@pytest.mark.django_db
def test_tampered_or_foreign_link_is_refused(client, open_signup):
    client.post(reverse("signup:start"), GOOD)
    path = _path(_link(mail.outbox[0].body))
    assert client.get(path[:-6] + "abcde/").status_code == 404
    pending = PendingSignup.objects.get()
    forged = signing.dumps({"p": pending.pk, "n": "0" * 32}, salt=signup.SALT)  # right signature, wrong nonce
    assert client.get(reverse("signup:verify", args=[forged])).status_code == 404
    other_salt = signing.dumps({"p": pending.pk, "n": pending.nonce}, salt="something-else")
    assert client.get(reverse("signup:verify", args=[other_salt])).status_code == 404


@pytest.mark.django_db
def test_honeypot_looks_successful_but_stores_nothing(client, open_signup):
    r = client.post(reverse("signup:start"), {**GOOD, signup.HONEYPOT: "http://spam.example"})
    assert r.status_code == 302 and r.url == reverse("signup:sent")
    assert not PendingSignup.objects.exists() and mail.outbox == []


@pytest.mark.django_db
def test_rate_limit_per_ip(client, open_signup):
    open_signup.SIGNUP_RATE_PER_HOUR = 2
    for i in range(2):
        r = client.post(reverse("signup:start"), {**GOOD, "email": f"p{i}@harborfreight.example"})
        assert r.status_code == 302
    r = client.post(reverse("signup:start"), {**GOOD, "email": "p9@harborfreight.example"})
    assert r.status_code == 429 and b"Too many sign-up attempts" in r.content
    assert PendingSignup.objects.count() == 2
    # Another network is not affected.
    r = client.post(reverse("signup:start"), {**GOOD, "email": "other@harborfreight.example"},
                    REMOTE_ADDR="203.0.113.50")
    assert r.status_code == 302


@pytest.mark.django_db
def test_validation_errors_and_django_password_validators(client, open_signup):
    r = client.post(reverse("signup:start"), {"company_name": "", "full_name": "D", "email": "not-an-email",
                                              "password": "12345678"})
    html = r.content.decode()
    assert r.status_code == 400
    assert "Enter your company" in html and "Enter a valid email address" in html
    assert "too short" in html or "too common" in html or "entirely numeric" in html
    r = client.post(reverse("signup:start"), {**GOOD, "password": "dana@harborfreight"})  # like the email
    assert r.status_code == 400 and "too similar" in r.content.decode()
    assert not PendingSignup.objects.exists()


@pytest.mark.django_db
def test_disposable_domains_are_refused_unless_switched_off(client, open_signup):
    r = client.post(reverse("signup:start"), {**GOOD, "email": "x@mailinator.com"})
    assert r.status_code == 400 and b"Throwaway email addresses" in r.content
    r = client.post(reverse("signup:start"), {**GOOD, "email": "x@eu.mailinator.com"})  # subdomains too
    assert r.status_code == 400
    open_signup.SIGNUP_BLOCK_DISPOSABLE = False
    assert client.post(reverse("signup:start"), {**GOOD, "email": "x@mailinator.com"}).status_code == 302
    open_signup.SIGNUP_BLOCKED_DOMAINS = ["competitor.example"]
    assert client.post(reverse("signup:start"), {**GOOD, "email": "spy@competitor.example"}).status_code == 400


@pytest.mark.django_db
def test_existing_address_gets_a_sign_in_email_and_no_new_account(client, open_signup, admin_user):
    r = client.post(reverse("signup:start"), {**GOOD, "email": admin_user.email.upper()})
    assert r.status_code == 302 and r.url == reverse("signup:sent")   # same page: no account enumeration
    assert not PendingSignup.objects.exists()
    assert len(mail.outbox) == 1 and "already have an account" in mail.outbox[0].body


@pytest.mark.django_db
def test_slug_collisions_get_a_number(client, open_signup, org):
    Organization.objects.create(name="Acme", slug="acme")
    Organization.objects.create(name="Acme 2", slug="acme-2")
    assert signup.unique_slug("ACME") == "acme-3"
    assert signup.unique_slug("Test") == "test-co"           # reserved word
    assert signup.unique_slug("Demo") == "demo-co"           # a demo organization's slug
    assert signup.unique_slug("!!!") == "company"
    assert signup.unique_slug("Ünïcode Shipping GmbH") == "unicode-shipping-gmbh"
    for n, email in enumerate(["a@acme.example", "b@acme.example"]):
        client.post(reverse("signup:start"), {**GOOD, "company_name": "Acme", "email": email})
        client.post(_path(_link(mail.outbox[n].body)), {"password": GOOD["password"]})
        client.logout()
    slugs = sorted(Organization.objects.filter(name="Acme").values_list("slug", flat=True))
    assert slugs == ["acme", "acme-3", "acme-4"]


@pytest.mark.django_db
def test_two_pending_signups_for_one_address_create_one_account(client, open_signup):
    client.post(reverse("signup:start"), GOOD)
    client.post(reverse("signup:start"), {**GOOD, "company_name": "Harbor Freight Two"})
    first, second = _path(_link(mail.outbox[0].body)), _path(_link(mail.outbox[1].body))
    assert client.post(first, {"password": GOOD["password"]}).status_code == 302
    client.logout()
    r = client.post(second, {"password": GOOD["password"]})
    assert r.status_code == 200 and b"Your account is ready" in r.content
    assert get_user_model().objects.filter(email=GOOD["email"]).count() == 1


@pytest.mark.django_db
def test_verification_emails_per_address_are_capped(client, open_signup):
    open_signup.SIGNUP_RATE_PER_HOUR = 100
    client.post(reverse("signup:start"), GOOD)
    for _ in range(5):
        client.post(reverse("signup:resend"), {"email": GOOD["email"]})
    assert len(mail.outbox) == signup.MAX_EMAILS_PER_ADDRESS_PER_HOUR


@pytest.mark.django_db
def test_purge_removes_expired_unverified_signups(open_signup):
    old = PendingSignup.objects.create(company_name="Old", full_name="O", email="o@old.example", password_hash="x",
                                       nonce="n" * 32)
    PendingSignup.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=5))
    PendingSignup.objects.create(company_name="New", full_name="N", email="n@new.example", password_hash="x",
                                 nonce="m" * 32)
    assert signup.purge() == 1
    assert list(PendingSignup.objects.values_list("company_name", flat=True)) == ["New"]


@pytest.mark.django_db
def test_onboarding_items_tick_from_real_state_and_can_be_hidden(client, open_signup, dataset):
    from apps.accounting.models import QBOConnection
    from apps.documents.services.ingest import ingest_bytes

    client.post(reverse("signup:start"), GOOD)
    client.post(_path(_link(mail.outbox[0].body)), {"password": GOOD["password"]})
    org = Organization.objects.get(name=GOOD["company_name"])
    ingest_bytes(org, "ci.pdf", (dataset / "pdf" / "S01_1_commercial_invoice.pdf").read_bytes(), process="none")
    QBOConnection.objects.create(organization=org, realm_id="1", access_token="a", refresh_token="b",
                                 access_expires_at=timezone.now())
    html = client.get(reverse("core:dashboard")).content.decode()
    assert "2 of 4 done" in html
    # Not shown to a reviewer.
    reviewer = get_user_model().objects.create_user("rev@x.example", "rev@x.example", "pw-123456789-test")
    Membership.objects.create(user=reviewer, organization=org, role=Membership.Role.REVIEWER)
    html = client.get(reverse("core:dashboard")).content.decode()
    assert "3 of 4 done" in html                    # a colleague joined
    other = client.__class__()
    other.force_login(reviewer)
    assert "Get started" not in other.get(reverse("core:dashboard")).content.decode()
    client.post(reverse("billing:dismiss_onboarding"))
    assert "Get started" not in client.get(reverse("core:dashboard")).content.decode()
    assert AuditEvent.objects.filter(organization=org, action="onboarding.dismissed").exists()


@pytest.mark.django_db
def test_operator_created_org_has_no_checklist(client, admin_user, org):
    client.force_login(admin_user)
    assert "Get started" not in client.get(reverse("core:dashboard")).content.decode()
