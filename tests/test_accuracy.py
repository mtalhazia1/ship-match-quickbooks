"""Accuracy from reviewer corrections, and changing a document's type."""
import pytest
from django.urls import reverse

from apps.core import accuracy
from apps.documents.models import Document
from apps.documents.services.ingest import ingest_bytes
from apps.shipments.models import Shipment


@pytest.fixture
def loaded(org, dataset):
    for f in ["S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.pdf", "S01_3_freight_invoice.pdf"]:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    return org


@pytest.mark.django_db
def test_corrections_count_as_errors_only_after_approval(client, user, approver, loaded):
    shipment = Shipment.objects.get(organization=loaded)
    doc = shipment.documents.get(doc_type="freight_invoice")
    client.force_login(user)
    client.post(reverse("review:update_field", args=[doc.pk]), {"name": "invoice_number", "value": "FIXED-1"})
    assert accuracy.build(loaded).documents == 0  # not reviewed yet

    shipment.refresh_from_db()
    for issue in shipment.issues.filter(resolved=False):
        issue.resolved = True
        issue.save()
    client.force_login(approver)
    client.post(reverse("review:approve", args=[shipment.pk]))
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.APPROVED

    r = accuracy.build(loaded)
    stat = next(s for s in r.by_field if s.doc_type == "freight_invoice" and s.name == "invoice_number")
    assert (stat.total, stat.wrong) == (1, 1)
    assert r.documents == 3 and r.documents_all_correct == 2
    assert r.recent[0]["new"] == "FIXED-1"
    assert client.get(reverse("core:accuracy")).status_code == 200


@pytest.mark.django_db
def test_reviewer_can_change_document_type(client, user, loaded):
    doc = Document.objects.get(organization=loaded, doc_type="freight_invoice")
    client.force_login(user)
    r = client.post(reverse("review:set_type", args=[doc.pk]), {"doc_type": "commercial_invoice"}, follow=True)
    doc.refresh_from_db()
    assert doc.doc_type == "commercial_invoice" and doc.classification_confidence == 1.0
    assert "read the document again" in r.content.decode()
    assert doc.fields.filter(name="invoice_number").exists()
