"""Wave 2 features working together: customs duty in landed cost, shared invoices through the posting
refactor (blockers and per-shipment lines), Xero in bulk posting, the firm view and onboarding."""
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounting.models import QBOConnection, XeroConnection
from apps.accounting.services import posting
from apps.billing import onboarding
from apps.landed.services import allocation
from apps.landed.services import charges as lc_charges
from apps.shipments.models import Shipment
from apps.workflow.services import decisions

from .test_customs import entry_shipment  # noqa: F401  (fixture)
from .test_landed import shared  # noqa: F401  (fixture)


@pytest.mark.django_db
def test_customs_duty_and_fees_count_in_landed_cost(entry_shipment):  # noqa: F811
    charges, notes = lc_charges.collect(entry_shipment)
    duty = [c for c in charges if c.source == "Customs entry"]
    assert duty, notes
    assert {c.code for c in duty} >= {"customs_duty", "merchandise_processing_fee"}
    assert all(c.amount > 0 and "entry" in c.description for c in duty)


@pytest.mark.django_db
def test_shared_invoice_posts_one_line_per_shipment_share(shared, user):  # noqa: F811
    doc, ships = shared["doc"], shared["ships"]
    allocation.confirm(doc, user)
    lines = posting.bill_lines(doc)
    refs = {s.reference for s in ships}
    memos = {line.memo.replace("Shipment ", "") for line in lines if line.memo}
    assert memos == refs
    total = Decimal(str(doc.field("total_amount")))
    assert sum(line.amount for line in lines) == total


@pytest.mark.django_db
def test_post_shipment_itself_waits_for_every_shipment_of_a_shared_invoice(shared, user):  # noqa: F811
    doc, primary = shared["doc"], shared["ships"][0]
    allocation.confirm(doc, user)
    Shipment.objects.filter(pk=primary.pk).update(status="approved")
    primary.refresh_from_db()
    with pytest.raises(posting.PostingBlocked, match="must be approved before it is posted"):
        posting.post_shipment(primary)


@pytest.mark.django_db
def test_bulk_post_and_onboarding_accept_xero(org, approver):
    s = Shipment.objects.create(organization=org, bl_number="BL1", status=Shipment.Status.APPROVED)
    assert "Connect QuickBooks or Xero" in decisions.post_blockers(s, approver)[0]
    assert not onboarding.ACCOUNTING_CHECKS or not any(check(org) for check in onboarding.ACCOUNTING_CHECKS)
    XeroConnection.objects.create(organization=org, tenant_id="tenant-1", access_expires_at=timezone.now())
    assert decisions.post_blockers(s, approver) == []
    assert any(check(org) for check in onboarding.ACCOUNTING_CHECKS)
    XeroConnection.objects.filter(organization=org).update(needs_reconnect=True)
    assert "Xero needs to be connected again" in decisions.post_blockers(s, approver)[0]
    assert not QBOConnection.objects.filter(organization=org).exists()
