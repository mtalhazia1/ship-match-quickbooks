"""PDFs holding several invoices: split into one document per invoice; one long invoice is never split."""
import pytest
from django.urls import reverse

from apps.core.models import AuditEvent
from apps.documents.models import Document
from apps.documents.services import llm
from apps.documents.services.ingest import ingest_bytes
from apps.documents.services.ocr import read_text
from apps.documents.services.pipeline import process_document
from apps.intake.services import splitting
from apps.shipments.models import Shipment
from synthetic import extra

INVOICE_A = """Harborlink Logistics LLC FREIGHT INVOICE
Invoice No.: A-1001
B/L No.: OSLN1000000001
Ocean Freight 2,150.00
TOTAL DUE: 2,150.00"""
BILL = """Oceanic Star Line BILL OF LADING
B/L No.: OSLN1000000001
Shipper: Brightway Electronics Co., Ltd.
Port of Loading: Ningbo"""
REMARKS = """Remarks
Goods delivered in good order.
Signed for receipt."""


@pytest.mark.django_db
def test_batch_of_three_invoices_is_split(org):
    pdf, truths = extra.batch(3)
    parent, _ = ingest_bytes(org, "harborlink-batch.pdf", pdf, process="sync")
    assert parent.status == Document.Status.SPLIT and parent.page_count == 3
    parts = list(parent.children.order_by("id"))
    assert [p.original_filename for p in parts] == [
        f"harborlink-batch (part {n} of 3, page {n}).pdf" for n in (1, 2, 3)]
    for part, truth in zip(parts, truths):
        assert part.status == Document.Status.MATCHED and part.doc_type == "freight_invoice"
        assert part.page_count == 1 and part.intake["part"]["pages"] == [part.intake["part"]["index"]] * 2
        assert part.field("invoice_number") == truth["invoice_number"]
        assert part.field("total_amount") == truth["total_amount"]
    assert len({p.match.shipment_id for p in parts}) == 3  # each invoice belongs to its own B/L
    reasons = parent.intake["split"]["boundaries"][0]["reasons"]
    assert any("invoice number changes" in r for r in reasons)
    assert AuditEvent.objects.filter(action="document.split", object_id=str(parent.pk)).exists()
    assert not hasattr(parent, "match")


@pytest.mark.parametrize("variant", [
    {"page_numbers": True}, {"page_numbers": False}, {"repeat_header": True}, {"repeat_header": True, "page_numbers": False},
])
@pytest.mark.django_db
def test_one_invoice_over_several_pages_is_never_split(org, variant):
    pdf, truth = extra.multipage_invoice(pages=3, **variant)
    doc, _ = ingest_bytes(org, "long-invoice.pdf", pdf, process="sync")
    assert doc.status != Document.Status.SPLIT and not doc.children.exists()
    assert doc.page_count == 3 and doc.field("invoice_number") == truth["invoice_number"]
    assert doc.field("total_amount") == truth["total_amount"]


def test_page_rules():
    assert splitting.page_info("Original Invoice No.: X-1001\nCredit Note No.: CN-77").printed_number == "CN-77"
    assert splitting.page_info("Invoice No. Date Amount").number is None  # a column heading, not a number
    info = splitting.page_info("INVOICE\nInvoice No.: A-1\nline\nPage 2 of 3")
    assert info.page_of == (2, 3) and info.title
    assert splitting.page_info("Charges\nSubtotal carried forward: 4,000.00").ends_with_total is False
    assert splitting.page_info("lines\nTOTAL DUE: 4,000.00").ends_with_total is True
    # A different number starts a new document; the same number continues it.
    assert [b.page for b in splitting.find_boundaries([INVOICE_A, INVOICE_A.replace("A-1001", "A-1002")])] == [1]
    assert splitting.find_boundaries([INVOICE_A, "INVOICE (continued)\nInvoice No.: A-1001\nmore lines"]) == []


def test_uncertain_boundary_follows_the_ai(settings, monkeypatch):
    """Invoice + B/L copy: two medium signals. Without AI they split; with AI the answer decides."""
    pages = [INVOICE_A, BILL]
    assert splitting.plan(pages)[0] == [(0, 0), (1, 1)]
    settings.EXTRACTION_PROVIDER, settings.ANTHROPIC_API_KEY = "anthropic", "k"
    answers, calls = [], []

    def fake_call(system, user, schema, name="record", timeout=120.0, client=None, pdf=None, purpose="extract"):
        calls.append((purpose, schema, user))
        return answers.pop(0)
    monkeypatch.setattr(llm, "structured_call", fake_call)
    answers.append({"pages": [{"page": 1, "starts_new_document": True, "document_number": "A-1001"},
                              {"page": 2, "starts_new_document": False, "document_number": None}]})
    assert splitting.plan(pages)[0] == [(0, 1)]
    purpose, schema, user = calls[-1]
    assert purpose == "split" and schema["additionalProperties"] is False
    assert schema["properties"]["pages"]["items"]["additionalProperties"] is False and '<page number="2">' in user
    # A weak signal (only a total on the previous page) splits only when the AI confirms it.
    weak = [INVOICE_A, REMARKS]
    answers.append({"pages": [{"page": 1, "starts_new_document": True, "document_number": None},
                              {"page": 2, "starts_new_document": True, "document_number": None}]})
    ranges, info = splitting.plan(weak)
    assert ranges == [(0, 0), (1, 1)] and info["ai_checked"]
    answers.append(None)
    monkeypatch.setattr(llm, "structured_call", lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("down")))
    assert splitting.plan(weak)[0] == [(0, 1)]  # AI down: rules only, weak signals don't split
    # Clear cases never cost an AI call.
    calls.clear()
    monkeypatch.setattr(llm, "structured_call", fake_call)
    assert splitting.plan([INVOICE_A, INVOICE_A.replace("A-1001", "A-1002")])[0] == [(0, 0), (1, 1)]
    assert not calls


@pytest.mark.django_db
def test_scanned_batch_reuses_ocr_text(org, settings, monkeypatch):
    settings.OCR_PROVIDER, settings.ANTHROPIC_API_KEY = "anthropic", "k"
    pdfs = [extra.freight_invoice(f"HA-30{n}", f"OSLN30000000{n}", [extra.make_container("OSL", 300100 + n)])[0]
            for n in range(3)]
    transcript = "\f".join(read_text(p).text for p in pdfs)
    calls = []

    def transcribe(pdf, client=None, timeout=180.0):
        calls.append(1)
        return transcript
    monkeypatch.setattr(llm, "transcribe_pdf", transcribe)
    parent, _ = ingest_bytes(org, "scan.tiff", extra.tiff_scan(pdfs), process="sync")
    assert parent.status == Document.Status.SPLIT and parent.children.count() == 3
    assert len(calls) == 1  # the parts reuse the pages' text instead of paying for OCR again
    for child in parent.children.all():
        assert child.text_source == "anthropic" and child.status == Document.Status.MATCHED


@pytest.mark.django_db
def test_processing_again_does_not_split_twice(org):
    pdf, _ = extra.batch(2)
    parent, _ = ingest_bytes(org, "batch.pdf", pdf, process="sync")
    assert process_document(parent.pk).status == Document.Status.SPLIT
    assert parent.children.count() == 2


@pytest.mark.django_db
def test_keep_as_one_document(client, user, viewer, org):
    pdf, truths = extra.batch(3)
    parent, _ = ingest_bytes(org, "batch.pdf", pdf, process="sync")
    assert Shipment.objects.filter(organization=org).count() == 3
    client.force_login(viewer)
    page = client.get(reverse("review:document", args=[parent.pk])).content.decode()
    assert "This PDF held 3 separate documents" in page and "Keep as one document" not in page
    assert client.post(reverse("intake:keep_whole", args=[parent.pk])).status_code == 403

    client.force_login(user)
    assert "Keep as one document" in client.get(reverse("review:document", args=[parent.pk])).content.decode()
    r = client.post(reverse("intake:keep_whole", args=[parent.pk]), follow=True)
    assert "is one document again. Its 3 parts were removed" in r.content.decode()
    parent.refresh_from_db()
    assert parent.status == Document.Status.MATCHED and parent.intake["keep_whole"]
    assert not parent.children.exists()
    assert parent.field("invoice_number") == truths[0]["invoice_number"]
    assert Shipment.objects.filter(organization=org).count() == 1  # the parts' empty shipments are gone
    assert AuditEvent.objects.filter(action="document.unsplit", object_id=str(parent.pk), actor=user).exists()
    assert process_document(parent.pk).status != Document.Status.SPLIT  # stays whole when read again


@pytest.mark.django_db
def test_keep_as_one_is_blocked_by_an_approved_part(client, user, org):
    pdf, _ = extra.batch(2)
    parent, _ = ingest_bytes(org, "batch.pdf", pdf, process="sync")
    shipment = parent.children.first().match.shipment
    shipment.status = Shipment.Status.APPROVED
    shipment.save()
    client.force_login(user)
    page = client.get(reverse("review:document", args=[parent.pk])).content.decode()
    assert "can't be joined again" in page
    r = client.post(reverse("intake:keep_whole", args=[parent.pk]), follow=True)
    assert "Not changed." in r.content.decode() and "Reopen it first" in r.content.decode()
    assert parent.children.count() == 2


@pytest.mark.django_db
def test_parts_count_as_uploaded_by_the_uploader(org, user):
    """Maker-checker: whoever uploaded the batch prepared each of its parts."""
    from apps.shipments.services.approval import makers

    pdf, _ = extra.batch(2)
    parent, _ = ingest_bytes(org, "batch.pdf", pdf, actor=user, process="sync")
    for part in parent.children.all():
        assert AuditEvent.objects.get(action="document.received", object_id=str(part.pk)).actor == user
        assert user.pk in makers(part.match.shipment)


@pytest.mark.django_db
def test_split_can_be_turned_off(org, settings):
    settings.INTAKE_SPLIT_PDFS = False
    pdf, _ = extra.batch(2)
    doc, _ = ingest_bytes(org, "batch.pdf", pdf, process="sync")
    assert doc.status != Document.Status.SPLIT and not doc.children.exists()
