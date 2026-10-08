"""Review workflow: approval gate, corrections, manual moves."""
import pytest
from django.urls import reverse

from apps.documents.models import Document
from apps.documents.services.ingest import ingest_bytes
from apps.shipments.models import Shipment


@pytest.fixture
def loaded(org, dataset):
    files = ["S03_1_commercial_invoice.pdf", "S03_2_bill_of_lading.pdf", "S03_3_freight_invoice.pdf",
             "S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.pdf", "S01_3_freight_invoice.pdf"]
    for f in files:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    return org


@pytest.mark.django_db
def test_error_blocks_approval_until_resolved(client, user, approver, approver2, loaded):
    bad = Shipment.objects.get(organization=loaded, issues__code="total_mismatch")
    client.force_login(approver2)
    client.post(reverse("review:approve", args=[bad.pk]))
    bad.refresh_from_db()
    assert bad.status == Shipment.Status.NEEDS_REVIEW

    # The reviewer accepts warnings; an approver overrides the error with a reason.
    client.force_login(user)
    for issue in bad.issues.filter(resolved=False, severity="warning"):
        client.post(reverse("review:resolve_issue", args=[issue.pk]))
    client.force_login(approver)
    for issue in bad.issues.filter(resolved=False, severity="error"):
        client.post(reverse("review:resolve_issue", args=[issue.pk]), {"note": "Checked with the supplier by email."})
    assert not bad.issues.filter(resolved=False).exists()

    # Maker-checker: the approver who overrode the error can't approve; a second approver can.
    client.post(reverse("review:approve", args=[bad.pk]))
    bad.refresh_from_db()
    assert bad.status == Shipment.Status.READY
    client.force_login(approver2)
    client.post(reverse("review:approve", args=[bad.pk]))
    bad.refresh_from_db()
    assert bad.status == Shipment.Status.APPROVED


@pytest.mark.django_db
def test_clean_shipment_is_ready(loaded):
    good = Shipment.objects.exclude(issues__code="total_mismatch").get(organization=loaded)
    assert good.status == Shipment.Status.READY
    assert good.links.count() == 3


@pytest.mark.django_db
def test_correcting_bl_number_moves_document(client, user, loaded):
    client.force_login(user)
    s1, s3 = sorted(Shipment.objects.filter(organization=loaded), key=lambda s: s.pk)
    doc = Document.objects.filter(match__shipment=s1, doc_type="freight_invoice").first()
    target_bl = s3.bl_number
    r = client.post(reverse("review:update_field", args=[doc.pk]), {"name": "bl_number", "value": target_bl}, follow=True)
    doc.refresh_from_db()
    assert doc.match.shipment_id == s3.pk
    assert doc.fields.get(name="bl_number").source == "human"
    assert f"now matches {s3.reference}" in r.content.decode()


@pytest.mark.django_db
def test_unchanged_value_is_not_recorded(client, user, loaded):
    from apps.core.models import AuditEvent

    client.force_login(user)
    doc = Document.objects.filter(organization=loaded, doc_type="bill_of_lading").first()
    before = AuditEvent.objects.filter(action="field.corrected").count()
    client.post(reverse("review:update_field", args=[doc.pk]), {"name": "bl_number", "value": doc.field("bl_number")})
    assert AuditEvent.objects.filter(action="field.corrected").count() == before
    assert doc.fields.get(name="bl_number").source != "human"


@pytest.mark.django_db
def test_other_org_cannot_see_shipment(client, db, loaded, django_user_model):
    stranger = django_user_model.objects.create_user("stranger", password="pw-123456789")
    client.force_login(stranger)
    s = Shipment.objects.filter(organization=loaded).first()
    assert client.get(reverse("review:shipment", args=[s.pk])).status_code == 404


@pytest.mark.django_db
def test_same_file_twice_is_stored_once(org, dataset):
    data = (dataset / "pdf" / "S01_1_commercial_invoice.pdf").read_bytes()
    _, first = ingest_bytes(org, "a.pdf", data, process="none")
    _, second = ingest_bytes(org, "b.pdf", data, process="none")
    assert first and not second
    assert Document.objects.filter(organization=org).count() == 1
