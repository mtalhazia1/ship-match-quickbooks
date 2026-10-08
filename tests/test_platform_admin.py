"""Platform superusers manage the platform, never a client's data: access to an organization is by membership only."""
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.core.models import AuditEvent, Membership, Organization
from apps.core.permissions import has_perm, role_for
from apps.core.utils import orgs_for_user
from apps.shipments.models import Shipment
from apps.shipments.services import approval

from .conftest import PASSWORD


@pytest.fixture
def root(django_user_model):
    return django_user_model.objects.create_superuser("root", "root@example.com", PASSWORD)


@pytest.fixture
def shipment(org):
    return Shipment.objects.create(organization=org, bl_number="MSCU1234567", status=Shipment.Status.READY)


@pytest.mark.django_db
def test_superuser_has_no_role_in_an_organization_it_does_not_belong_to(root, org):
    assert role_for(root, org) is None
    assert not has_perm(root, org, "view")
    assert not orgs_for_user(root).exists()


@pytest.mark.django_db
def test_superuser_cannot_open_or_change_another_organizations_records(client, root, org, shipment):
    client.force_login(root)
    assert client.get(reverse("review:shipment", args=[shipment.pk])).status_code == 404
    assert client.get(reverse("core:dashboard") + f"?org={org.slug}").status_code == 404
    r = client.post(reverse("review:approve", args=[shipment.pk]), {"note": "x"})
    assert r.status_code == 404
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.READY and shipment.approved_by is None
    assert client.get(f"/api/{org.slug}/shipments").status_code == 404


@pytest.mark.django_db
def test_superuser_member_gets_exactly_its_role_and_approval_limit(root, org, shipment, monkeypatch):
    Membership.objects.create(user=root, organization=org, role=Membership.Role.REVIEWER)
    assert role_for(root, org) == "reviewer" and not has_perm(root, org, "approve")

    Membership.objects.filter(user=root).update(role=Membership.Role.APPROVER, approval_limit=Decimal("50.00"))
    root._membership_cache = {}
    totals = approval.Totals()
    totals.home = Decimal("100.00")
    monkeypatch.setattr(approval, "shipment_totals", lambda s: totals)
    assert any("above your approval limit" in r for r in approval.approval_blockers(shipment, root))


@pytest.mark.django_db
def test_client_data_is_read_only_in_the_platform_admin(client, root, org, shipment):
    client.force_login(root)
    assert client.get(reverse("admin:shipments_shipment_change", args=[shipment.pk])).status_code == 200   # can look
    assert client.get(reverse("admin:shipments_shipment_add")).status_code == 403
    r = client.post(reverse("admin:shipments_shipment_change", args=[shipment.pk]),
                    {"organization": org.pk, "status": "approved", "bl_number": "CHANGED"})
    assert r.status_code == 403
    assert client.post(reverse("admin:shipments_shipment_delete", args=[shipment.pk]), {"post": "yes"}).status_code == 403
    shipment.refresh_from_db()
    assert shipment.bl_number == "MSCU1234567" and shipment.status == Shipment.Status.READY
    assert client.get(reverse("admin:documents_document_add")).status_code == 403


@pytest.mark.django_db
def test_organization_controls_are_locked_after_creation(client, root, org):
    client.force_login(root)
    page = client.get(reverse("admin:core_organization_change", args=[org.pk])).content.decode()
    assert 'name="maker_checker"' not in page and 'name="require_mfa"' not in page and 'name="name"' in page
    assert client.post(reverse("admin:core_organization_delete", args=[org.pk]), {"post": "yes"}).status_code == 403
    assert Organization.objects.filter(pk=org.pk).exists()


@pytest.mark.django_db
def test_support_access_through_the_admin_is_in_the_clients_audit_log(client, root, org, shipment):
    client.force_login(root)
    r = client.post(reverse("admin:core_membership_add"),
                    {"user": root.pk, "organization": org.pk, "role": "viewer", "approval_limit": ""})
    assert r.status_code == 302
    added = AuditEvent.objects.get(organization=org, action="team.platform_added")
    assert added.actor == root and added.data["user"] == "root" and added.data["role"] == "viewer"

    assert client.get(reverse("review:shipment", args=[shipment.pk])).status_code == 200   # now a viewer
    assert client.post(reverse("review:approve", args=[shipment.pk]), {"note": "x"}).status_code in (302, 403)
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.READY

    m = Membership.objects.get(user=root, organization=org)
    client.post(reverse("admin:core_membership_delete", args=[m.pk]), {"post": "yes"})
    assert AuditEvent.objects.filter(organization=org, action="team.platform_removed").exists()
    assert client.get(reverse("review:shipment", args=[shipment.pk])).status_code == 404
