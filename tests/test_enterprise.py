"""Roles, approval controls, security and administration."""
import time
from decimal import Decimal

import pyotp
import pytest
from django.urls import reverse

from apps.accounts.models import ApiKey
from apps.accounts.services import mfa
from apps.accounts.services.apikeys import create_key
from apps.core.models import AuditEvent, Membership, Organization
from apps.documents.models import Document
from apps.documents.services.ingest import ingest_bytes
from apps.shipments.models import Shipment, ValidationIssue
from apps.shipments.services.approval import approval_blockers

from .conftest import PASSWORD


@pytest.fixture
def loaded(org, dataset):
    for f in ["S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.pdf", "S01_3_freight_invoice.pdf",
              "S03_1_commercial_invoice.pdf", "S03_2_bill_of_lading.pdf", "S03_3_freight_invoice.pdf"]:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    return org


def _ready(org):
    return Shipment.objects.filter(organization=org, status=Shipment.Status.READY).first()


def _messages(response) -> str:
    return " ".join(str(m) for m in response.context["messages"]) if response.context else ""


# ---------------------------------------------------------------- roles


@pytest.mark.django_db
def test_viewer_cannot_edit_or_upload(client, viewer, loaded):
    client.force_login(viewer)
    doc = Document.objects.filter(organization=loaded).first()
    assert client.post(reverse("review:update_field", args=[doc.pk]), {"name": "bl_number", "value": "X"}).status_code == 403
    assert client.post(reverse("review:upload")).status_code == 403
    assert client.get(reverse("review:queue")).status_code == 200


@pytest.mark.django_db
def test_reviewer_cannot_override_error_or_approve(client, user, loaded):
    client.force_login(user)
    error = ValidationIssue.objects.filter(organization=loaded, severity="error", resolved=False).first()
    r = client.post(reverse("review:resolve_issue", args=[error.pk]), {"note": "Looks fine to me, honestly."})
    assert r.status_code == 403
    error.refresh_from_db()
    assert not error.resolved
    assert client.post(reverse("review:approve", args=[_ready(loaded).pk])).status_code == 403


@pytest.mark.django_db
def test_override_needs_a_reason(client, approver, loaded):
    client.force_login(approver)
    error = ValidationIssue.objects.filter(organization=loaded, severity="error", resolved=False).first()
    client.post(reverse("review:resolve_issue", args=[error.pk]), {"note": "ok"})
    error.refresh_from_db()
    assert not error.resolved
    client.post(reverse("review:resolve_issue", args=[error.pk]), {"note": "Supplier confirmed the total by email."})
    error.refresh_from_db()
    assert error.resolved and error.resolution_note.startswith("Supplier")
    event = AuditEvent.objects.get(action="issue.resolved", object_id=str(error.pk))
    assert event.data["severity"] == "error" and event.actor == approver


@pytest.mark.django_db
def test_maker_checker_can_be_turned_off(client, approver, loaded):
    shipment = _ready(loaded)
    doc = shipment.documents.filter(doc_type="commercial_invoice").first()
    client.force_login(approver)
    client.post(reverse("review:update_field", args=[doc.pk]), {"name": "invoice_date", "value": "2026-01-02"})
    shipment.refresh_from_db()
    assert any("maker-checker" in b for b in approval_blockers(shipment, approver))
    loaded.maker_checker = False
    loaded.save()
    shipment.refresh_from_db()
    shipment.organization.refresh_from_db()
    approver._membership_cache = {}
    assert approval_blockers(shipment, approver) == []


@pytest.mark.django_db
def test_approval_limit_and_missing_exchange_rate(client, approver, loaded):
    m = Membership.objects.get(user=approver)
    m.approval_limit = Decimal("10.00")
    m.save()
    shipment = _ready(loaded)
    client.force_login(approver)
    r = client.post(reverse("review:approve", args=[shipment.pk]), follow=True)
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.READY
    text = _messages(r)
    assert "approval limit" in text or "exchange rate" in text


@pytest.mark.django_db
def test_reject_needs_reason_and_reopen(client, approver, loaded):
    shipment = _ready(loaded)
    client.force_login(approver)
    client.post(reverse("review:reject", args=[shipment.pk]), {"note": ""})
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.READY
    client.post(reverse("review:reject", args=[shipment.pk]), {"note": "Wrong vessel on the freight invoice."})
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.REJECTED
    client.post(reverse("review:reopen", args=[shipment.pk]))
    shipment.refresh_from_db()
    assert shipment.status in {Shipment.Status.READY, Shipment.Status.NEEDS_REVIEW}


# ---------------------------------------------------------------- uploads


@pytest.mark.django_db
def test_upload_messages_are_specific(client, user, org, dataset):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client.force_login(user)
    pdf = (dataset / "pdf" / "S01_2_bill_of_lading.pdf").read_bytes()
    r = client.post(reverse("review:upload"), {"files": [SimpleUploadedFile("bl.pdf", pdf, "application/pdf")]}, follow=True)
    assert "Received 1 document: bl.pdf" in _messages(r)
    r = client.post(reverse("review:upload"), {"files": [SimpleUploadedFile("again.pdf", pdf, "application/pdf"),
                                                         SimpleUploadedFile("notes.txt", b"hello", "text/plain")]},
                    follow=True)
    text = _messages(r)
    assert "was uploaded before" in text and "not a PDF" in text and "Received 0" not in text


# ---------------------------------------------------------------- API


@pytest.mark.django_db
def test_api_key_scoped_to_its_organization(client, admin_user, loaded):
    key, token = create_key(loaded, "ERP", ApiKey.Role.VIEWER, admin_user, None)
    auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    assert client.get(f"/api/{loaded.slug}/shipments", **auth).status_code == 200
    other = Organization.objects.create(name="Other", slug="other")
    assert client.get(f"/api/{other.slug}/shipments", **auth).status_code == 404
    assert client.post(f"/api/{loaded.slug}/documents", **auth).status_code in (403, 422)
    assert client.get(f"/api/{loaded.slug}/shipments", HTTP_AUTHORIZATION="Bearer sm_bad_token").status_code == 401
    key.revoked_at = key.created_at
    key.save()
    assert client.get(f"/api/{loaded.slug}/shipments", **auth).status_code == 401


@pytest.mark.django_db
def test_api_rate_limit(client, settings, admin_user, org):
    settings.API_RATE_LIMIT_PER_MINUTE = 2
    _, token = create_key(org, "ERP", ApiKey.Role.VIEWER, admin_user, None)
    codes = [client.get(f"/api/{org.slug}/shipments", HTTP_AUTHORIZATION=f"Bearer {token}").status_code for _ in range(3)]
    assert codes[:2] == [200, 200] and codes[2] == 429


# ---------------------------------------------------------------- sign-in security


@pytest.mark.django_db
def test_login_lockout_after_repeated_failures(client, user, settings):
    settings.LOGIN_MAX_FAILURES_PER_USER = 3
    for _ in range(3):
        client.post(reverse("accounts:login"), {"username": "reviewer", "password": "wrong-password"})
    r = client.post(reverse("accounts:login"), {"username": "reviewer", "password": PASSWORD}, follow=True)
    assert "Too many failed sign-in attempts" in _messages(r)
    assert "_auth_user_id" not in client.session
    assert AuditEvent.objects.filter(action="auth.login_failed").count() == 3


@pytest.mark.django_db
def test_two_factor_sign_in(client, user):
    secret, _ = mfa.start_enrollment(user)
    codes = mfa.confirm_enrollment(user, pyotp.TOTP(secret).now())
    assert codes and len(codes) == 10
    r = client.post(reverse("accounts:login"), {"username": "reviewer", "password": PASSWORD})
    assert r.status_code == 302 and r["Location"] == reverse("accounts:verify")
    assert "_auth_user_id" not in client.session
    client.post(reverse("accounts:verify"), {"code": "000000"})
    assert "_auth_user_id" not in client.session
    # A code already used during enrollment can't be replayed; use a recovery code instead.
    r = client.post(reverse("accounts:verify"), {"code": codes[0]})
    assert r.status_code == 302 and client.session["_auth_user_id"] == str(user.pk)
    assert len(mfa.profile_for(user).recovery_codes) == 9


@pytest.mark.django_db
def test_org_can_require_two_factor(client, user, org):
    org.require_mfa = True
    org.save()
    client.force_login(user)
    r = client.get(reverse("review:queue"))
    assert r.status_code == 302 and r["Location"] == reverse("accounts:security")
    assert client.get(reverse("accounts:security")).status_code == 200


@pytest.mark.django_db
def test_idle_session_is_signed_out(client, user, settings):
    client.force_login(user)
    session = client.session
    session["last_activity"] = int(time.time()) - settings.SESSION_IDLE_TIMEOUT - 5
    session.save()
    r = client.get(reverse("review:queue"))
    assert r.status_code == 302 and reverse("accounts:login") in r["Location"]


@pytest.mark.django_db
def test_admin_login_uses_app_sign_in(client):
    r = client.get("/admin/login/?next=/admin/")
    assert r.status_code == 302 and r["Location"].startswith(reverse("accounts:login"))


@pytest.mark.django_db
def test_security_headers(client, user):
    client.force_login(user)
    r = client.get(reverse("core:dashboard"))
    assert "script-src 'self'" in r["Content-Security-Policy"]
    assert r["X-Request-ID"]
    assert r["X-Frame-Options"] == "SAMEORIGIN"


# ---------------------------------------------------------------- administration


@pytest.mark.django_db
def test_team_invite_and_admin_guards(client, admin_user, org):
    client.force_login(admin_user)
    r = client.post(reverse("core:invite"), {"email": "new.person@example.com", "name": "New Person",
                                             "role": "approver", "approval_limit": "25,000"}, follow=True)
    m = Membership.objects.get(user__email="new.person@example.com")
    assert m.role == "approver" and m.approval_limit == Decimal("25000.00")
    assert not m.user.has_usable_password()
    assert "/account/password/set/" in r.content.decode()
    own = Membership.objects.get(user=admin_user)
    r = client.post(reverse("core:update_member", args=[own.pk]), {"role": "viewer"}, follow=True)
    own.refresh_from_db()
    assert own.role == "admin" and "own role" in _messages(r)


@pytest.mark.django_db
def test_reviewer_cannot_manage(client, user):
    client.force_login(user)
    for name in ("core:team", "core:settings", "core:api_keys", "core:audit"):
        assert client.get(reverse(name)).status_code == 403


@pytest.mark.django_db
def test_settings_change_is_audited(client, admin_user, org):
    client.force_login(admin_user)
    client.post(reverse("core:settings"), {"name": org.name, "home_currency": "USD", "timezone": "Asia/Karachi",
                                           "review_threshold": "0.9", "fx_rates": "EUR=1.10", "maker_checker": "on"})
    org.refresh_from_db()
    assert org.timezone == "Asia/Karachi" and org.fx_rates == {"EUR": "1.10"} and org.review_threshold == 0.9
    event = AuditEvent.objects.get(action="settings.updated")
    assert set(event.data["changes"]) >= {"timezone", "fx_rates", "review_threshold"}


@pytest.mark.django_db
def test_audit_log_only_shows_own_organization(client, admin_user, org):
    other = Organization.objects.create(name="Other", slug="other")
    AuditEvent.objects.create(organization=other, action="shipment.approved", object_type="Shipment", object_id="99")
    client.force_login(admin_user)
    r = client.get(reverse("core:audit"))
    assert r.status_code == 200
    assert all(e.organization_id in (org.pk, None) for e in r.context["page"])


@pytest.mark.django_db
def test_pages_render_for_every_role(client, user, viewer, approver, admin_user, loaded):
    shipment = Shipment.objects.filter(organization=loaded).first()
    pages = [reverse("core:dashboard"), reverse("review:queue") + "?status=all", reverse("review:documents"),
             reverse("review:search") + "?q=S0", reverse("review:shipment", args=[shipment.pk]),
             reverse("accounts:security")]
    for u in (viewer, user, approver, admin_user):
        client.force_login(u)
        for p in pages:
            assert client.get(p).status_code == 200, (u.username, p)
