"""Team management for organization admins: invite, change role or limit, remove, reset 2FA."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.views.decorators.http import require_POST

from apps.core.models import Membership, Organization
from apps.core.money import AmountError, parse_amount
from apps.core.permissions import require
from apps.core.utils import audit, current_org, use_org

from . import invitations
from .services import mfa

MAX_LIMIT = Decimal("999999999999.99")   # Membership.approval_limit is DecimalField(max_digits=14, decimal_places=2)
SHARED_ACCOUNT = ("{name} also belongs to other organizations, so only {name} can change the password or "
                  "two-factor setup of this account. They can use “Forgot your password?” on the sign-in page.")


def is_shared(membership: Membership) -> bool:
    """True when the person's account belongs to any other organization too. An admin of one organization must
    not be able to reset the credentials of an account the other organizations rely on."""
    user = membership.user
    if user.is_staff or user.is_superuser:   # platform staff are not any one organization's to reset
        return True
    return Membership.objects.filter(user_id=membership.user_id).exclude(organization_id=membership.organization_id).exists()


def _limit(raw: str) -> Decimal | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = parse_amount(raw, allow_negative=True, limit=MAX_LIMIT)
    except AmountError as e:
        raise ValueError("Approval limit is too large." if e.kind == "range"
                         else "Approval limit must be a number, or empty for no limit.") from None
    if value < 0:
        raise ValueError("Approval limit can't be negative.")
    return value


def set_password_link(request, user) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return request.build_absolute_uri(reverse("accounts:password_reset_confirm", args=[uid, token]))


@login_required
def team(request):
    org = current_org(request)
    require(request.user, org, "manage")
    members = list(Membership.objects.filter(organization=org).select_related("user", "user__profile")
                   .order_by("user__first_name", "user__username"))
    for m in members:
        m.shared = is_shared(m)   # such a person's sign-in time and credentials are not this organization's to see or reset
    return render(request, "team/list.html", {
        "members": members, "roles": Membership.Role.choices,
        "invite_link": request.session.pop("invite_link", None),
    })


@login_required
@require_POST
def invite(request):
    org = current_org(request)
    require(request.user, org, "manage")
    email = request.POST.get("email", "").strip().lower()
    name = request.POST.get("name", "").strip()
    role = request.POST.get("role", Membership.Role.REVIEWER)
    try:
        validate_email(email)
        if role not in Membership.Role.values:
            raise ValueError("Choose a role.")
        limit = _limit(request.POST.get("approval_limit", ""))
    except (ValidationError, ValueError) as e:
        messages.error(request, e.messages[0] if isinstance(e, ValidationError) else str(e))
        return redirect("core:team")
    from apps.billing.usage import seat_limit_message

    full = seat_limit_message(org)
    if full:
        messages.error(request, full)
        return redirect("core:team")

    User = get_user_model()
    inviter = request.user.get_full_name() or request.user.get_username()
    with transaction.atomic():
        user = User.objects.filter(email__iexact=email).first() or User.objects.filter(username__iexact=email).first()
        if user is not None and Membership.objects.filter(user=user, organization=org).exists():
            messages.info(request, f"{email} is already a member of {org.name}.")
            return redirect("core:team")
        # A person nobody else depends on (no account yet, or an account that lost its last organization and was
        # deactivated) gets a fresh account and sets their own password.
        fresh = user is None or (not user.is_active and not user.is_superuser and not user.memberships.exists())
        if user is None:
            first, _, last = name.partition(" ")
            user = User(username=email, email=email, first_name=first[:150], last_name=last[:150])
        if fresh:
            user.is_active = True
            user.set_unusable_password()
            user.save()
            mfa.disable(user)   # no old two-factor secret survives into the new account
            membership = Membership.objects.create(user=user, organization=org, role=role, approval_limit=limit)

    if fresh:
        audit(org, "team.invited", membership, actor=request.user, email=email, role=role, limit=limit)
        link = set_password_link(request, user)
        send_mail(
            subject=f"You're invited to {org.name} on ShipMatch",
            message=(f"{inviter} invited you to review shipments "
                     f"for {org.name}.\n\nSet your password here (the link works for 3 days):\n{link}\n"),
            from_email=None, recipient_list=[email], fail_silently=True,
        )
    else:
        # The account exists and may belong to other organizations. It is not attached to this one until its
        # owner accepts, so nothing about it changes here and nothing about it is revealed to the inviter.
        link = invitations.link_for(request, invitations.make_token(org, user, role, limit, request.user))
        audit(org, "team.invitation_sent", user, actor=request.user, email=email, role=role, limit=limit)
        send_mail(
            subject=f"You're invited to {org.name} on ShipMatch",
            message=(f"{inviter} invited you to join {org.name} on ShipMatch as {Membership.Role(role).label.lower()}.\n\n"
                     f"Sign in as {email} and open this link to accept (it works for 7 days):\n{link}\n\n"
                     "If you weren't expecting this, ignore this email: nothing changes unless you accept."),
            from_email=None, recipient_list=[user.email or email], fail_silently=True,
        )
    # Same words and the same on-screen link either way, so the reply doesn't say whether the email has an account.
    request.session["invite_link"] = {"email": email, "link": link}
    messages.success(request, f"Invited {email}. We emailed them a link; you can also copy it below.")
    return redirect("core:team")


@login_required
def accept_invite(request, token):
    """The invited person accepts (POST) after seeing who invited them and what they would be able to do."""
    try:
        data = invitations.read_token(token)
    except signing.BadSignature:
        return render(request, "team/invite_invalid.html", {"reason": "expired"}, status=400)
    org = Organization.objects.filter(pk=data["o"]).first()
    inviter_is_admin = Membership.objects.filter(
        user_id=data["b"], organization_id=data["o"], role=Membership.Role.ADMIN, user__is_active=True).exists()
    if org is None or not inviter_is_admin:
        return render(request, "team/invite_invalid.html", {"reason": "withdrawn"}, status=400)
    if request.user.pk != data["u"]:
        return render(request, "team/invite_invalid.html", {"reason": "other_account", "org": org}, status=403)
    if Membership.objects.filter(user=request.user, organization=org).exists():
        use_org(request, org)
        messages.info(request, f"You're already a member of {org.name}.")
        return redirect("core:dashboard")
    role = data["r"] if data["r"] in Membership.Role.values else Membership.Role.REVIEWER
    limit = Decimal(data["l"]) if data["l"] is not None else None
    inviter = get_user_model().objects.filter(pk=data["b"]).first()
    if request.method == "POST":
        from apps.billing.usage import seat_limit_message

        full = seat_limit_message(org)
        if full:
            messages.error(request, full)
            return redirect("core:dashboard")
        membership = Membership.objects.create(user=request.user, organization=org, role=role, approval_limit=limit)
        audit(org, "team.joined", membership, actor=request.user, role=role, limit=limit, invited_by=data["b"])
        use_org(request, org)
        messages.success(request, f"You joined {org.name}.")
        return redirect("core:dashboard")
    return render(request, "team/accept_invite.html", {
        "org": org, "role_label": Membership.Role(role).label, "limit": limit,
        "inviter": (inviter.get_full_name() or inviter.get_username()) if inviter else "An administrator"})


def _admins_left(org, excluding: Membership) -> int:
    return Membership.objects.filter(organization=org, role=Membership.Role.ADMIN, user__is_active=True
                                     ).exclude(pk=excluding.pk).count()


@login_required
@require_POST
def update_member(request, pk):
    org = current_org(request)
    require(request.user, org, "manage")
    m = get_object_or_404(Membership, pk=pk, organization=org)
    role = request.POST.get("role", m.role)
    try:
        if role not in Membership.Role.values:
            raise ValueError("Choose a role.")
        if m.user_id == request.user.pk and role != m.role:
            raise ValueError("You can't change your own role. Ask another admin.")
        if m.role == Membership.Role.ADMIN and role != Membership.Role.ADMIN and _admins_left(org, m) == 0:
            raise ValueError("Every organization needs at least one admin.")
        limit = _limit(request.POST.get("approval_limit", ""))
    except ValueError as e:
        messages.error(request, str(e))
        return redirect("core:team")
    old = {"role": m.role, "limit": m.approval_limit}
    m.role, m.approval_limit = role, limit
    m.save(update_fields=["role", "approval_limit"])
    audit(org, "team.updated", m, actor=request.user, user=m.user.get_username(),
          old_role=old["role"], role=role, old_limit=old["limit"], limit=limit)
    messages.success(request, f"Updated {m.user.get_full_name() or m.user.get_username()}.")
    return redirect("core:team")


@login_required
@require_POST
def remove_member(request, pk):
    org = current_org(request)
    require(request.user, org, "manage")
    m = get_object_or_404(Membership, pk=pk, organization=org)
    if m.user_id == request.user.pk:
        messages.error(request, "You can't remove yourself. Ask another admin.")
    elif m.role == Membership.Role.ADMIN and _admins_left(org, m) == 0:
        messages.error(request, "Every organization needs at least one admin.")
    else:
        user = m.user
        m.delete()
        if not user.memberships.exists() and not user.is_superuser:
            user.is_active = False
            user.save(update_fields=["is_active"])
        audit(org, "team.removed", user, actor=request.user, user=user.get_username())
        messages.success(request, f"Removed {user.get_full_name() or user.get_username()} from {org.name}.")
    return redirect("core:team")


@login_required
@require_POST
def reset_member_mfa(request, pk):
    org = current_org(request)
    require(request.user, org, "manage")
    m = get_object_or_404(Membership, pk=pk, organization=org)
    if is_shared(m):
        messages.error(request, SHARED_ACCOUNT.format(name=m.user.get_full_name() or m.user.get_username()))
        return redirect("core:team")
    mfa.disable(m.user)
    audit(org, "team.mfa_reset", m.user, actor=request.user, user=m.user.get_username())
    messages.success(request, f"Two-factor authentication reset for {m.user.get_username()}. "
                              "They will set it up again at next sign-in if your organization requires it.")
    return redirect("core:team")


@login_required
@require_POST
def send_password_link(request, pk):
    org = current_org(request)
    require(request.user, org, "manage")
    m = get_object_or_404(Membership, pk=pk, organization=org)
    if is_shared(m):
        messages.error(request, SHARED_ACCOUNT.format(name=m.user.get_full_name() or m.user.get_username()))
        return redirect("core:team")
    link = set_password_link(request, m.user)
    if m.user.email:
        send_mail(subject="Set your ShipMatch password", message=f"Set a new password here (works for 3 days):\n{link}\n",
                  from_email=None, recipient_list=[m.user.email], fail_silently=True)
    request.session["invite_link"] = {"email": m.user.email or m.user.get_username(), "link": link}
    audit(org, "team.password_link", m.user, actor=request.user, user=m.user.get_username())
    messages.success(request, "Password link created. It was emailed if the user has an email address.")
    return redirect("core:team")
