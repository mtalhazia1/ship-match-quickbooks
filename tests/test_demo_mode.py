"""DEMO_MODE: banner, demo accounts on the sign-in page, guard rails, nightly reset."""
import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.services import mfa
from apps.core.models import AuditEvent, Membership, Organization

BANNER = "Demo environment. Data resets every night."


@pytest.fixture
def demo(db, settings):
    """A DEMO_MODE server seeded like a fresh demo (without documents, to keep the tests quick)."""
    settings.DEMO_MODE = True
    settings.DEMO_ORGS = ["demo"]
    call_command("seed_demo", verbosity=0)
    return Organization.objects.get(slug="demo")


def _login(client, username):
    client.force_login(get_user_model().objects.get(username=username))


def _flash(response) -> str:
    return " ".join(str(m) for m in response.context["messages"]) if response.context else ""


# --------------------------------------------------------------------------- banner and sign-in page


@pytest.mark.django_db
def test_banner_and_demo_accounts_only_in_demo_mode(client, settings):
    settings.DEMO_MODE = False
    page = client.get(reverse("accounts:login")).content.decode()
    assert BANNER not in page and "reviewer-demo-pass" not in page and "demo.js" not in page
    settings.DEMO_MODE = True
    page = client.get(reverse("accounts:login")).content.decode()
    assert BANNER in page
    assert 'data-demo-username="reviewer"' in page and 'data-demo-password="reviewer-demo-pass"' in page
    assert "js/demo.js" in page and "<script>" not in page          # no inline scripts (CSP)


@pytest.mark.django_db
def test_banner_on_app_pages(client, demo):
    _login(client, "reviewer")
    assert BANNER in client.get(reverse("core:dashboard")).content.decode()
    assert BANNER in client.get(reverse("review:queue")).content.decode()


@pytest.mark.django_db
def test_demo_account_can_sign_in_with_listed_password(client, demo):
    r = client.post(reverse("accounts:login"), {"username": "approver", "password": "approver-demo-pass"})
    assert r.status_code == 302 and r["Location"] == reverse("core:dashboard")


# --------------------------------------------------------------------------- guard rails


@pytest.mark.django_db
def test_demo_user_cannot_change_password(client, demo):
    _login(client, "reviewer")
    r = client.post(reverse("accounts:password_change"), {
        "old_password": "reviewer-demo-pass", "new_password1": "a-brand-new-pass-123", "new_password2": "a-brand-new-pass-123",
    }, follow=True)
    assert "passwords can&#x27;t be changed" in r.content.decode() or "passwords can't be changed" in _flash(r)
    assert get_user_model().objects.get(username="reviewer").check_password("reviewer-demo-pass")


@pytest.mark.django_db
def test_demo_user_cannot_change_two_factor(client, demo):
    _login(client, "approver")
    for name in ("accounts:mfa_start", "accounts:mfa_disable", "accounts:mfa_recovery_codes"):
        r = client.post(reverse(name), {"password": "approver-demo-pass"}, follow=True)
        assert "two-factor settings of the demo accounts" in _flash(r)
    assert not mfa.profile_for(get_user_model().objects.get(username="approver")).mfa_secret


@pytest.mark.django_db
def test_demo_admin_cannot_remove_members_or_touch_demo_accounts(client, demo):
    _login(client, "admin")
    member = Membership.objects.get(organization=demo, user__username="reviewer")
    r = client.post(reverse("core:remove_member", args=[member.pk]), follow=True)
    assert "removing team members is turned off" in _flash(r)
    assert Membership.objects.filter(pk=member.pk).exists()
    r = client.post(reverse("core:update_member", args=[member.pk]), {"role": "viewer"}, follow=True)
    assert "keep their roles" in _flash(r)
    member.refresh_from_db()
    assert member.role == "reviewer"
    for name in ("core:reset_member_mfa", "core:password_link"):
        client.post(reverse(name, args=[member.pk]))
    assert not AuditEvent.objects.filter(action__in=["team.mfa_reset", "team.password_link"]).exists()


@pytest.mark.django_db
def test_demo_admin_can_still_manage_invited_members(client, demo):
    _login(client, "admin")
    client.post(reverse("core:invite"), {"email": "visitor@example.org", "role": "reviewer"})
    invited = Membership.objects.get(organization=demo, user__username="visitor@example.org")
    client.post(reverse("core:update_member", args=[invited.pk]), {"role": "approver", "approval_limit": "1000"})
    invited.refresh_from_db()
    assert invited.role == "approver"


@pytest.mark.django_db
def test_invites_blocked_when_real_email_is_configured(client, demo, settings):
    settings.EMAIL_HOST = "smtp.example.com"
    _login(client, "admin")
    r = client.post(reverse("core:invite"), {"email": "someone@example.org", "role": "reviewer"}, follow=True)
    assert "would send real email" in _flash(r)
    assert not Membership.objects.filter(user__username="someone@example.org").exists()


@pytest.mark.django_db
def test_demo_admin_cannot_disconnect_quickbooks(client, demo):
    from datetime import timedelta

    from django.utils import timezone

    from apps.accounting.models import QBOConnection

    QBOConnection.objects.create(organization=demo, realm_id="123", access_token="a", refresh_token="r",
                                 access_expires_at=timezone.now() + timedelta(hours=1))
    _login(client, "admin")
    r = client.post(reverse("accounting:disconnect", args=[demo.pk]), follow=True)
    assert "QuickBooks can&#x27;t be disconnected" in r.content.decode() or "can't be disconnected" in _flash(r)
    assert QBOConnection.objects.filter(organization=demo).exists()


@pytest.mark.django_db
def test_demo_admin_cannot_change_security_settings_but_can_change_others(client, demo):
    _login(client, "admin")
    base = {"name": demo.name, "home_currency": "USD", "timezone": "UTC", "maker_checker": "on"}
    r = client.post(reverse("core:settings"), {**base, "require_mfa": "on"}, follow=True)
    assert "can&#x27;t be changed" in r.content.decode() or "can't be changed" in _flash(r)
    demo.refresh_from_db()
    assert demo.require_mfa is False
    r = client.post(reverse("core:settings"), {**base, "name": "Acme demo renamed"}, follow=True)
    demo.refresh_from_db()
    assert demo.name == "Acme demo renamed" and demo.maker_checker is True


@pytest.mark.django_db
def test_password_reset_of_demo_accounts_is_blocked(client, demo, mailoutbox):
    r = client.post(reverse("accounts:password_reset"), {"email": "reviewer@example.com"}, follow=True)
    assert "can&#x27;t be reset" in r.content.decode() or "can't be reset" in _flash(r)
    assert len(mailoutbox) == 0
    user = get_user_model().objects.get(username="reviewer")
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    client.post(reverse("accounts:password_reset_confirm", args=[uid, "set-password"]),
                {"new_password1": "x-new-password-123", "new_password2": "x-new-password-123"})
    user.refresh_from_db()
    assert user.check_password("reviewer-demo-pass")


@pytest.mark.django_db
def test_demo_admin_has_no_platform_powers(client, demo):
    admin = get_user_model().objects.get(username="admin")
    admin.is_superuser = admin.is_staff = True   # as with seed_demo --superuser; demo mode never lets it act as one
    admin.save()
    sandbox = Organization.objects.create(name="Someone's try upload", slug="try-abc")
    _login(client, "admin")
    r = client.get(reverse("admin:index"), follow=True)
    assert r.redirect_chain and "/admin/" not in r.redirect_chain[-1][0]
    page = client.get(reverse("core:dashboard") + f"?org={sandbox.slug}").content.decode()
    assert "Someone&#x27;s try upload" not in page and "Someone's try upload" not in page


@pytest.mark.django_db
def test_rules_apply_only_in_demo_mode_and_only_to_demo_accounts(client, org, admin_user, user, settings):
    settings.DEMO_MODE = False
    member = Membership.objects.get(user=user)
    client.force_login(admin_user)
    client.post(reverse("core:remove_member", args=[member.pk]))
    assert not Membership.objects.filter(pk=member.pk).exists()
    # In demo mode a real (non-demo) admin is not limited either.
    settings.DEMO_MODE = True
    other = Membership.objects.create(user=get_user_model().objects.create_user("x", password="pw-123456789-test"),
                                      organization=org, role="viewer")
    client.post(reverse("core:remove_member", args=[other.pk]))
    assert not Membership.objects.filter(pk=other.pk).exists()


# --------------------------------------------------------------------------- reset_demo


@pytest.mark.django_db
def test_reset_demo_refuses_without_demo_mode(settings):
    settings.DEMO_MODE = False
    with pytest.raises(CommandError, match="DEMO_MODE=1"):
        call_command("reset_demo")


@pytest.mark.django_db
def test_reset_demo_wipes_and_rebuilds(client, demo, settings, org):
    from apps.documents.models import Document
    from apps.learning.models import DocumentLearning, VendorProfile
    from apps.shipments.models import Shipment

    # A visitor changed things during the day.
    reviewer = get_user_model().objects.get(username="reviewer")
    reviewer.set_password("changed-by-a-visitor-1")
    reviewer.is_active = False
    reviewer.save()
    mfa.start_enrollment(reviewer)
    visitor = get_user_model().objects.create_user("visitor@example.org")
    Membership.objects.create(user=visitor, organization=demo, role="viewer")
    demo.require_mfa = True
    demo.save()
    customer_org_id = org.pk

    call_command("reset_demo", "--shipments", "4", verbosity=0)

    new = Organization.objects.get(slug="demo")
    assert new.pk != demo.pk and new.require_mfa is False
    assert Organization.objects.filter(pk=customer_org_id).exists()          # other organizations untouched
    reviewer.refresh_from_db()
    assert reviewer.is_active and reviewer.check_password("reviewer-demo-pass")
    assert not mfa.profile_for(reviewer).mfa_secret
    admin = get_user_model().objects.get(username="admin")
    assert admin.check_password("admin") and not admin.is_superuser and not admin.is_staff
    assert not get_user_model().objects.filter(username="visitor@example.org").exists()
    assert set(Membership.objects.filter(organization=new).values_list("user__username", flat=True)) == {
        "admin", "reviewer", "approver"}
    assert Document.objects.filter(organization=new).count() >= 4 * 2
    assert Shipment.objects.filter(organization=new).exists()
    # The vendor learning example: the broker's second invoice was read with what the first taught.
    profile = VendorProfile.objects.get(organization=new)
    assert profile.labels["invoice_number"]["label"] == "Ref No." and profile.date_format == "dmy"
    second = Document.objects.get(organization=new, original_filename="coastline-24206.pdf")
    assert second.field("invoice_number") == "CCB/24206"
    assert DocumentLearning.objects.get(document=second).fields
    assert AuditEvent.objects.filter(organization__isnull=True, action="demo.reset").exists()


@pytest.mark.django_db
def test_reset_demo_runs_seed_rates_when_installed(demo, monkeypatch):
    from apps.demo.services import reset

    calls = []
    monkeypatch.setattr(reset, "get_commands", lambda: {"seed_rates": "apps.money"})
    monkeypatch.setattr(reset, "call_command", lambda name, *a, **kw: calls.append((name, kw)))
    assert reset.seed_rates("demo") is True
    assert calls == [("seed_rates", {"org": "demo"})]
    monkeypatch.setattr(reset, "get_commands", lambda: {})
    assert reset.seed_rates("demo") is False


def test_nightly_reset_is_scheduled_only_in_demo_mode(monkeypatch):
    import importlib

    import config.settings as conf

    monkeypatch.setenv("DEMO_MODE", "1")
    on = importlib.reload(conf)
    assert on.CELERY_BEAT_SCHEDULE["reset-demo-nightly"]["task"] == "apps.demo.tasks.reset_demo"
    monkeypatch.setenv("DEMO_MODE", "0")
    off = importlib.reload(conf)
    assert "reset-demo-nightly" not in off.CELERY_BEAT_SCHEDULE
