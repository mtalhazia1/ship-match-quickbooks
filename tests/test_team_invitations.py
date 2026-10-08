"""QA-048: an organization admin must not be able to take over an account that belongs to other organizations.

Before: inviting an email that already had an account added that account to the inviting organization on the
spot. The admin could then send that account a password link (shown on screen) or reset its two-factor, which
is a way to take over any user whose email is known, including admins of other companies."""
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.accounts import invitations
from apps.accounts.services import mfa
from apps.core.models import AuditEvent, Membership, Organization

from .conftest import PASSWORD, _member

User = get_user_model()


def _msgs(response) -> str:
    return " ".join(str(m) for m in response.context["messages"])


@pytest.fixture
def other_org(db):
    return Organization.objects.create(name="Other Freight Co", slug="other-freight")


@pytest.fixture
def victim(db, other_org):
    """An admin of another company, with two-factor on."""
    u = _member(other_org, "victim", Membership.Role.ADMIN)
    secret, _ = mfa.start_enrollment(u)
    import pyotp

    assert mfa.confirm_enrollment(u, pyotp.TOTP(secret).now())
    return u


# ---------------------------------------------------------------- inviting someone who already has an account


@pytest.mark.django_db
def test_existing_account_is_not_attached_to_the_inviting_organization(client, admin_user, org, victim, mailoutbox):
    client.force_login(admin_user)

    r = client.post(reverse("core:invite"), {"email": victim.email, "role": "admin"}, follow=True)

    assert not Membership.objects.filter(user=victim, organization=org).exists()
    assert "Invited" in _msgs(r)
    victim.refresh_from_db()
    assert victim.check_password(PASSWORD) and victim.is_active
    # the owner is asked, by email, and the link is for them only
    mail = next(m for m in mailoutbox if victim.email in m.to)
    assert "/account/invitations/" in mail.body and "nothing changes unless you accept" in mail.body
    assert AuditEvent.objects.filter(action="team.invitation_sent", organization=org).exists()
    assert not AuditEvent.objects.filter(action="team.invited", organization=org).exists()


@pytest.mark.django_db
def test_reply_does_not_reveal_whether_the_email_has_an_account(client, admin_user, victim):
    client.force_login(admin_user)

    existing = client.post(reverse("core:invite"), {"email": victim.email, "role": "reviewer"}, follow=True)
    new = client.post(reverse("core:invite"), {"email": "nobody.yet@example.com", "role": "reviewer"}, follow=True)

    strip = lambda text, email: text.replace(email, "<email>")   # noqa: E731
    assert strip(_msgs(existing), victim.email) == strip(_msgs(new), "nobody.yet@example.com")
    # a copyable link is shown in both cases, so the page looks the same too
    assert "Link for" in existing.content.decode() and "Link for" in new.content.decode()


@pytest.mark.django_db
def test_new_person_still_gets_a_set_password_link(client, admin_user, org, mailoutbox):
    client.force_login(admin_user)

    r = client.post(reverse("core:invite"), {"email": "new.person@example.com", "name": "New Person",
                                             "role": "approver", "approval_limit": "25,000"}, follow=True)

    m = Membership.objects.get(user__email="new.person@example.com", organization=org)
    assert m.role == "approver" and m.approval_limit == Decimal("25000.00")
    assert not m.user.has_usable_password()
    assert "/account/password/set/" in r.content.decode()
    assert any("/account/password/set/" in mail.body for mail in mailoutbox)


@pytest.mark.django_db
def test_inviting_a_current_member_says_so(client, admin_user, org, user):
    client.force_login(admin_user)
    r = client.post(reverse("core:invite"), {"email": user.email, "role": "reviewer"}, follow=True)
    assert "already a member" in _msgs(r)


@pytest.mark.django_db
def test_deactivated_account_with_no_organization_starts_fresh(client, admin_user, org, other_org):
    ghost = _member(other_org, "ghost", Membership.Role.REVIEWER)
    Membership.objects.filter(user=ghost).delete()
    ghost.is_active = False
    ghost.save()
    client.force_login(admin_user)

    client.post(reverse("core:invite"), {"email": ghost.email, "role": "reviewer"})

    ghost.refresh_from_db()
    assert ghost.is_active and not ghost.has_usable_password()   # the old password does not come back
    assert Membership.objects.filter(user=ghost, organization=org).exists()


# ---------------------------------------------------------------- accepting


def _invitation(org, invitee, inviter, role="approver", limit=Decimal("5000.00")):
    return invitations.make_token(org, invitee, role, limit, inviter)


@pytest.mark.django_db
def test_invited_person_can_accept_with_the_role_and_limit_offered(client, admin_user, org, victim):
    token = _invitation(org, victim, admin_user)
    client.force_login(victim)

    page = client.get(reverse("accounts:accept_invite", args=[token]))
    assert page.status_code == 200 and org.name in page.content.decode()
    assert not Membership.objects.filter(user=victim, organization=org).exists()   # looking changes nothing

    r = client.post(reverse("accounts:accept_invite", args=[token]), follow=True)

    m = Membership.objects.get(user=victim, organization=org)
    assert (m.role, m.approval_limit) == ("approver", Decimal("5000.00"))
    assert "You joined" in _msgs(r)
    event = AuditEvent.objects.get(action="team.joined")
    assert event.actor_id == victim.pk and event.organization_id == org.pk


@pytest.mark.django_db
def test_someone_else_cannot_use_the_invitation(client, admin_user, org, victim, user):
    token = _invitation(org, victim, admin_user)
    client.force_login(user)   # a different signed-in person

    r = client.post(reverse("accounts:accept_invite", args=[token]))

    assert r.status_code == 403
    assert not Membership.objects.filter(user=victim, organization=org).exists()
    assert not Membership.objects.filter(user=user, organization=org, role="approver").exists()


@pytest.mark.django_db
def test_invitation_needs_sign_in(client, admin_user, org, victim):
    token = _invitation(org, victim, admin_user)
    r = client.get(reverse("accounts:accept_invite", args=[token]))
    assert r.status_code == 302 and "/account/login/" in r["Location"]


@pytest.mark.django_db
@pytest.mark.parametrize("damage", ["tampered", "expired", "garbage"])
def test_bad_or_old_invitations_do_nothing(client, admin_user, org, victim, monkeypatch, damage):
    token = _invitation(org, victim, admin_user)
    if damage == "tampered":
        token = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    elif damage == "expired":
        monkeypatch.setattr(invitations, "MAX_AGE_SECONDS", -1)
    else:
        token = "not-a-token"
    client.force_login(victim)

    r = client.post(reverse("accounts:accept_invite", args=[token]))

    assert r.status_code == 400
    assert not Membership.objects.filter(user=victim, organization=org).exists()


@pytest.mark.django_db
def test_invitation_dies_when_the_inviter_is_no_longer_an_admin(client, admin_user, org, victim):
    token = _invitation(org, victim, admin_user)
    Membership.objects.filter(user=admin_user, organization=org).update(role=Membership.Role.REVIEWER)
    client.force_login(victim)

    r = client.post(reverse("accounts:accept_invite", args=[token]))

    assert r.status_code == 400
    assert not Membership.objects.filter(user=victim, organization=org).exists()


@pytest.mark.django_db
def test_accepting_twice_is_harmless(client, admin_user, org, victim):
    token = _invitation(org, victim, admin_user)
    client.force_login(victim)
    client.post(reverse("accounts:accept_invite", args=[token]))
    r = client.post(reverse("accounts:accept_invite", args=[token]), follow=True)
    assert "already a member" in _msgs(r)
    assert Membership.objects.filter(user=victim, organization=org).count() == 1


# ---------------------------------------------------------------- credentials of shared accounts


@pytest.fixture
def shared(db, org, other_org, victim):
    """The victim has joined this organization too (by accepting), so this admin now sees them in the team."""
    return Membership.objects.create(user=victim, organization=org, role=Membership.Role.REVIEWER)


@pytest.mark.django_db
def test_admin_cannot_send_a_password_link_for_a_shared_account(client, admin_user, shared, victim, mailoutbox):
    client.force_login(admin_user)

    r = client.post(reverse("core:password_link", args=[shared.pk]), follow=True)

    assert "also belongs to other organizations" in _msgs(r)
    assert "invite_link" not in client.session
    assert not mailoutbox
    victim.refresh_from_db()
    assert victim.check_password(PASSWORD)


@pytest.mark.django_db
def test_admin_cannot_reset_two_factor_of_a_shared_account(client, admin_user, shared, victim):
    assert mfa.profile_for(victim).mfa_enabled
    client.force_login(admin_user)

    r = client.post(reverse("core:reset_member_mfa", args=[shared.pk]), follow=True)

    assert "also belongs to other organizations" in _msgs(r)
    assert mfa.profile_for(victim).mfa_enabled
    assert not AuditEvent.objects.filter(action="team.mfa_reset").exists()


@pytest.mark.django_db
def test_platform_staff_count_as_shared(client, admin_user, org):
    staff = _member(org, "staffer", Membership.Role.REVIEWER)
    staff.is_staff = True
    staff.save()
    m = Membership.objects.get(user=staff)
    client.force_login(admin_user)
    r = client.post(reverse("core:password_link", args=[m.pk]), follow=True)
    assert "also belongs to other organizations" in _msgs(r)


@pytest.mark.django_db
def test_single_organization_members_can_still_be_reset(client, admin_user, org, user, mailoutbox):
    secret, _ = mfa.start_enrollment(user)
    import pyotp

    mfa.confirm_enrollment(user, pyotp.TOTP(secret).now())
    m = Membership.objects.get(user=user, organization=org)
    client.force_login(admin_user)

    r = client.post(reverse("core:password_link", args=[m.pk]), follow=True)
    assert "Password link created" in _msgs(r) and mailoutbox
    client.post(reverse("core:reset_member_mfa", args=[m.pk]))
    assert not mfa.profile_for(user).mfa_enabled


@pytest.mark.django_db
def test_team_page_hides_other_organizations_details(client, admin_user, shared, victim):
    victim.last_login = "2026-10-04T13:17:00Z"
    victim.save()
    client.force_login(admin_user)

    html = client.get(reverse("core:team")).content.decode()

    assert "13:17" not in html   # when they last signed in elsewhere is not this organization's business
    assert "Also belongs to other organizations" in html
    assert reverse("core:password_link", args=[shared.pk]) not in html
    assert reverse("core:reset_member_mfa", args=[shared.pk]) not in html
