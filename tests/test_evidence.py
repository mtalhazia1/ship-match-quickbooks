"""Evidence highlighting: where each extracted value is printed on the PDF, and the review-screen hooks."""
import io
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfplumber
import pytest
from django.conf import settings
from django.core.management import CommandError, call_command
from django.urls import reverse
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from apps.core.models import Membership, Organization
from apps.documents.models import Document, ExtractedField, OcrLayout
from apps.documents.services import locate as L
from apps.documents.services.ingest import ingest_bytes
from apps.shipments.models import Shipment, ValidationIssue
from apps.shipments.services.evidence import doc_payload, issue_targets

PAGE_W, PAGE_H = A4


# --------------------------------------------------------------------------- helpers


def make_pdf(*pages) -> bytes:
    """Each page is a list of (x, y_from_top, text[, font, size])."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    for items in pages:
        for item in items:
            x, y, text = item[:3]
            c.setFont(item[3] if len(item) > 3 else "Helvetica", item[4] if len(item) > 4 else 10)
            c.drawString(x, PAGE_H - y, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def box_text(pdf_bytes: bytes, page: int, box) -> str:
    """Text printed fully inside a box (with half a point of slack)."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        p = pdf.pages[page - 1]
        w, h = p.width, p.height
        inside = p.within_bbox((box[0] * w - 0.5, box[1] * h - 0.5, box[2] * w + 0.5, box[3] * h + 0.5))
        return " ".join((inside.extract_text() or "").split())


def where(pdf_bytes: bytes, **values) -> dict:
    return L.locate_values(L.index_pdf(pdf_bytes), values)


def found(loc: dict) -> tuple[int, list]:
    assert loc is not None and loc["status"] == L.FOUND, loc
    assert loc["boxes"], loc
    for box in loc["boxes"]:
        assert all(0 <= v <= 1 for v in box) and box[0] < box[2] and box[1] < box[3], box
    return loc["page"], loc["boxes"]


def ground_truth(dataset) -> list[dict]:
    return json.loads((Path(dataset) / "ground_truth.json").read_text())["documents"]


# --------------------------------------------------------------------------- the synthetic dataset


def test_dataset_values_are_boxed_exactly(dataset):
    """Invoice number, total, a date, every container and the (multi-word) vendor or carrier name, on every
    readable synthetic document: the box contains exactly the printed value."""
    checked = 0
    for d in ground_truth(dataset):
        if d["scanned"]:
            continue
        data = (Path(dataset) / "pdf" / d["file"]).read_bytes()
        f = {k: v for k, v in d["fields"].items() if k != "layout_variant"}
        res = where(data, **f)
        if "invoice_number" in f:
            page, boxes = found(res["invoice_number"])
            assert L.norm(box_text(data, page, boxes[0])) == L.norm(f["invoice_number"])
        if "total_amount" in f:
            page, boxes = found(res["total_amount"])
            assert L.to_decimal(box_text(data, page, boxes[0])) == Decimal(f["total_amount"]), d["file"]
        date_field = "invoice_date" if "invoice_date" in f else "issue_date"
        page, boxes = found(res[date_field])
        assert L.to_date(box_text(data, page, boxes[0])) == date.fromisoformat(f[date_field]), d["file"]
        name_field = "vendor_name" if "vendor_name" in f else "carrier_name"
        page, boxes = found(res[name_field])
        assert L.norm(box_text(data, page, boxes[0])) == L.norm(f[name_field])
        assert boxes[0][1] < 0.1, "the letterhead copy at the top wins over 'Please remit to ...' at the bottom"
        items = res["container_numbers"]["items"]
        assert [i["key"] for i in items] == [L.norm(c) for c in f["container_numbers"]]
        for item in items:
            assert L.norm(box_text(data, item["page"], item["boxes"][0])) == item["key"]
        checked += 1
    assert checked >= 40


def test_dataset_line_items_get_description_and_amount(dataset):
    d = next(x for x in ground_truth(dataset) if x["doc_type"] == "freight_invoice" and not x["scanned"])
    data = (Path(dataset) / "pdf" / d["file"]).read_bytes()
    loc = where(data, line_items=d["fields"]["line_items"])["line_items"]
    assert loc["status"] == L.FOUND
    assert len(loc["items"]) == len(d["fields"]["line_items"])
    for item, truth in zip(loc["items"], d["fields"]["line_items"]):
        assert box_text(data, item["page"], item["boxes"][0]) == truth["description"]
        assert L.to_decimal(box_text(data, item["page"], item["amount"][0])) == Decimal(truth["amount"])


def test_scanned_page_gets_no_box(dataset):
    d = next(x for x in ground_truth(dataset) if x["scanned"])
    data = (Path(dataset) / "pdf" / d["file"]).read_bytes()
    res = where(data, invoice_number="X-1", total_amount="10.00", container_numbers=["MSCU1234565"])
    assert {loc["status"] for loc in res.values()} == {L.SCANNED}
    assert not any(loc.get("boxes") for loc in res.values())


# --------------------------------------------------------------------------- formats


@pytest.mark.parametrize("printed", [
    "1,234.50", "1234.5", "1234.50", "1.234,50", "1 234,50", "1'234.50", "$1,234.50", "USD1,234.50", "1,234.50 USD",
])
def test_amount_formats(printed):
    data = make_pdf([(50, 80, "Invoice No.: INV-1"), (330, 300, "Total:"), (420, 300, printed)])
    page, boxes = found(where(data, total_amount="1234.50")["total_amount"])
    text = box_text(data, page, boxes[0])
    assert L.to_decimal(text) == Decimal("1234.50")
    assert "Total" not in text and "USD" not in text and "$" not in text  # the box is the number itself


def test_currency_code_in_its_own_word_is_not_boxed():
    data = make_pdf([(330, 300, "Total due:"), (420, 300, "EUR"), (450, 300, "1.234,50")])
    page, boxes = found(where(data, total_amount="1234.5")["total_amount"])
    assert box_text(data, page, boxes[0]) == "1.234,50"


def test_numbers_inside_references_and_dates_are_not_amounts():
    data = make_pdf([(50, 80, "PO No.: PO-2026-1019"), (50, 96, "Date: 04/13/2026"), (50, 112, "Ref: X2026")])
    assert where(data, total_amount="2026.00")["total_amount"]["status"] == L.NOT_FOUND


def test_negative_amount_in_brackets():
    data = make_pdf([(330, 300, "Credit total:"), (420, 300, "(250.00)")])
    page, boxes = found(where(data, total_amount="-250.00")["total_amount"])
    assert box_text(data, page, boxes[0]) == "250.00"


@pytest.mark.parametrize("printed", [
    "2026-03-04", "03/04/2026", "04/03/2026", "04.03.2026", "4 Mar 2026", "04-Mar-26", "March 4, 2026",
    "Mar 4, 2026", "4th March 2026", "2026/03/04",
])
def test_date_formats(printed):
    data = make_pdf([(50, 80, "Invoice Date:"), (160, 80, printed), (50, 96, "Due Date:"), (160, 96, "2026-04-03")])
    page, boxes = found(where(data, invoice_date="2026-03-04")["invoice_date"])
    assert box_text(data, page, boxes[0]) == printed


def test_month_and_year_alone_is_not_a_date():
    data = make_pdf([(50, 80, "Period: Mar 2026")])
    assert where(data, invoice_date="2026-03-20")["invoice_date"]["status"] == L.NOT_FOUND


def test_container_numbers_with_spaces_and_dashes_get_one_box_each():
    data = make_pdf([(50, 80, "Container(s):"), (150, 80, "MSCU 123456-5,"), (240, 80, "TGHU-765432-1"),
                     (50, 96, "PO No.:"), (150, 96, "PO-1001, PO-1002")])
    res = where(data, container_numbers=["MSCU1234565", "TGHU7654321"], po_numbers=["PO-1001", "PO-1002"])
    containers = res["container_numbers"]
    assert containers["status"] == L.FOUND
    assert [box_text(data, i["page"], i["boxes"][0]) for i in containers["items"]] == ["MSCU 123456-5", "TGHU-765432-1"]
    pos = res["po_numbers"]["items"]
    assert [box_text(data, i["page"], i["boxes"][0]) for i in pos] == ["PO-1001", "PO-1002"]


def test_list_with_one_value_missing_is_partial():
    data = make_pdf([(50, 80, "Container:"), (150, 80, "MSCU1234565")])
    loc = where(data, container_numbers=["MSCU1234565", "TGHU7654321"])["container_numbers"]
    assert loc["status"] == L.PARTIAL
    assert [i["key"] for i in loc["items"]] == ["MSCU1234565"]


def test_label_and_value_printed_as_one_word():
    data = make_pdf([(50, 80, "Invoice No.:INV-77")])
    page, boxes = found(where(data, invoice_number="INV-77")["invoice_number"])
    assert box_text(data, page, boxes[0]) == "INV-77"


def test_value_inside_a_longer_reference_is_not_matched():
    data = make_pdf([(50, 80, "Booking:"), (150, 80, "XINV-77A")])
    assert where(data, invoice_number="INV-77")["invoice_number"]["status"] == L.NOT_FOUND


def test_name_wrapped_over_two_lines_next_to_another_column():
    data = make_pdf([
        (50, 80, "Shipper:"), (130, 80, "Golden Harvest Trading"), (330, 80, "Consignee:"), (410, 80, "Acme Imports LLC"),
        (130, 94, "Company Limited"), (410, 94, "1200 Harbor Blvd"),
    ])
    page, boxes = found(where(data, shipper="Golden Harvest Trading Company Limited")["shipper"])
    assert [box_text(data, page, b) for b in boxes] == ["Golden Harvest Trading", "Company Limited"]


def test_name_printed_slightly_differently_is_found_fuzzily():
    data = make_pdf([(50, 60, "Brightway Electronics Co Ltd", "Helvetica-Bold", 14)])
    loc = where(data, vendor_name="Brightway Electronics Co., Ltd.")["vendor_name"]
    assert loc["status"] == L.FOUND  # letters and digits are the same: exact
    loc = where(data, vendor_name="Brightway Electronic Co Ltd")["vendor_name"]
    page, boxes = found(loc)
    assert loc["score"] < 0.8  # fuzzy matches are marked as less certain


# --------------------------------------------------------------------------- repeated values


REPEATED = [
    (50, 60, "Acme Freight Forwarding LLC", "Helvetica-Bold", 14),
    (50, 100, "Invoice No.:"), (150, 100, "AF-1001"),
    (50, 116, "B/L No.:"), (150, 116, "OSLN1234567890"),
    (50, 180, "Description"), (480, 180, "Amount"),
    (50, 200, "Handling"), (480, 200, "50.00"),
    (50, 216, "Storage"), (480, 216, "50.00"),
    (50, 232, "Ocean freight"), (480, 232, "900.00"),
    (350, 260, "Subtotal:"), (480, 260, "1,000.00"),
    (350, 276, "Total:"), (480, 276, "1,000.00", "Helvetica-Bold", 10),
    (50, 700, "Notes: freight for OSLN1234567890 is collect. Quote AF-1001 with payment."),
    (50, 760, "Please remit to Acme Freight Forwarding LLC."),
]


def test_repeated_values_prefer_labels_letterhead_and_the_total_line():
    data = make_pdf(REPEATED)
    res = where(data, vendor_name="Acme Freight Forwarding LLC", invoice_number="AF-1001",
                bl_number="OSLN1234567890", total_amount="1000.00")
    _, vendor = found(res["vendor_name"])
    assert vendor[0][1] < 0.1
    _, inv = found(res["invoice_number"])
    assert abs(inv[0][1] - 90 / PAGE_H) < 0.01      # the 'Invoice No.' line, not the note
    _, bl = found(res["bl_number"])
    assert abs(bl[0][1] - 106 / PAGE_H) < 0.01
    _, total = found(res["total_amount"])
    assert abs(total[0][1] - 266 / PAGE_H) < 0.01   # 'Total', not 'Subtotal'


def test_line_items_with_the_same_amount_get_different_boxes_even_out_of_order():
    data = make_pdf(REPEATED)
    items = [{"description": "Ocean freight", "amount": "900.00"}, {"description": "Handling", "amount": "50.00"},
             {"description": "Storage", "amount": "50.00"}]
    loc = where(data, total_amount="1000.00", line_items=items)["line_items"]
    assert loc["status"] == L.FOUND
    amounts = [i["amount"][0] for i in loc["items"]]
    assert len({round(a[1], 3) for a in amounts}) == 3
    texts = [box_text(data, i["page"], i["boxes"][0]) for i in loc["items"]]
    assert texts == ["Ocean freight", "Handling", "Storage"]


def test_value_not_on_the_page_gets_no_box():
    data = make_pdf(REPEATED)
    res = where(data, invoice_number="ZZ-9999", total_amount="999.99", invoice_date="2026-01-01")
    for loc in res.values():
        assert loc["status"] == L.NOT_FOUND and "boxes" not in loc


def test_multi_page_total_on_last_page():
    data = make_pdf([(50, 80, "Invoice No.: INV-5"), (50, 300, "Freight"), (480, 300, "700.00")],
                    [(350, 400, "Total due:"), (480, 400, "700.00")])
    page, _ = found(where(data, total_amount="700")["total_amount"])
    assert page == 2


# --------------------------------------------------------------------------- OCR word boxes (Textract)


def test_ocr_word_boxes_are_used_for_scans():
    words = [[1, 0.10, 0.10, 0.20, 0.12, "Invoice"], [1, 0.21, 0.10, 0.26, 0.12, "No.:"],
             [1, 0.30, 0.10, 0.42, 0.12, "HL-555001"], [1, 0.60, 0.80, 0.68, 0.82, "Total:"],
             [1, 0.80, 0.80, 0.90, 0.82, "1,234.50"]]
    res = L.locate_values(L.index_ocr(words, 1), {"invoice_number": "HL-555001", "total_amount": "1234.5"})
    assert res["invoice_number"]["source"] == "textract"
    assert res["invoice_number"]["boxes"] == [[0.3, 0.1, 0.42, 0.12]]
    assert res["total_amount"]["boxes"][0][0] == pytest.approx(0.8)


class FakeTextract:
    LINES = [("FREIGHT INVOICE", 0.06), ("Harborlink Logistics LLC", 0.09), ("Invoice No.: HL-555001", 0.14),
             ("B/L No.: OSLN1234567890", 0.17), ("Ocean freight 1,234.50", 0.30), ("Total Due: 1,234.50", 0.40)]

    def detect_document_text(self, Document):
        blocks = []
        for text, top in self.LINES:
            blocks.append({"BlockType": "LINE", "Text": text,
                           "Geometry": {"BoundingBox": {"Left": 0.1, "Top": top, "Width": 0.5, "Height": 0.02}}})
            left = 0.1
            for word in text.split():
                width = 0.012 * len(word)
                blocks.append({"BlockType": "WORD", "Text": word,
                               "Geometry": {"BoundingBox": {"Left": left, "Top": top, "Width": width, "Height": 0.02}}})
                left += width + 0.01
        return {"Blocks": blocks}


@pytest.mark.django_db
def test_textract_geometry_is_stored_and_used(org, dataset, monkeypatch):
    import boto3

    settings.OCR_PROVIDER = "textract"
    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeTextract())
    d = next(x for x in ground_truth(dataset) if x["scanned"])
    doc, _ = ingest_bytes(org, d["file"], (Path(dataset) / "pdf" / d["file"]).read_bytes(), process="sync")
    assert doc.text_source == "textract"
    assert OcrLayout.objects.get(document=doc).words
    inv = doc.fields.get(name="invoice_number")
    assert inv.location["source"] == "textract" and inv.location["status"] == L.FOUND
    left = 0.1 + 0.012 * 7 + 0.01 + 0.012 * 4 + 0.01   # third word of 'Invoice No.: HL-555001'
    assert inv.location["boxes"][0][0] == pytest.approx(left, abs=1e-3)
    assert inv.page == 1
    assert doc_payload(doc)["mode"] == "pdfjs"

    # A later re-locate (backfill, correction) reads the stored positions, not the image.
    ExtractedField.objects.filter(document=doc).update(location=None)
    assert L.safe_locate(doc)[L.FOUND] >= 3
    assert doc.fields.get(name="total_amount").location["source"] == "textract"


# --------------------------------------------------------------------------- pipeline, corrections, backfill


@pytest.fixture
def loaded(org, dataset):
    names = ["S03_1_commercial_invoice.pdf", "S03_2_bill_of_lading.pdf", "S03_3_freight_invoice.pdf"]
    for name in names:
        ingest_bytes(org, name, (Path(dataset) / "pdf" / name).read_bytes(), process="sync")
    return org


@pytest.mark.django_db
def test_pipeline_records_locations(loaded):
    doc = Document.objects.get(organization=loaded, original_filename="S03_3_freight_invoice.pdf")
    fields = list(doc.fields.all())
    assert fields and all(f.location and f.location["status"] == L.FOUND for f in fields)
    total = doc.fields.get(name="total_amount")
    assert total.page == 1 and total.location["score"] >= 0.9


@pytest.mark.django_db
def test_pipeline_survives_a_locate_failure(org, dataset, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("pdf parser exploded")

    monkeypatch.setattr(L, "locate_document", boom)
    name = "S01_3_freight_invoice.pdf"
    doc, _ = ingest_bytes(org, name, (Path(dataset) / "pdf" / name).read_bytes(), process="sync")
    assert doc.status == Document.Status.MATCHED and doc.error == ""
    assert doc.fields.count() > 5
    assert not doc.fields.exclude(location=None).exists()
    assert "Could not locate field values" in caplog.text


@pytest.mark.django_db
def test_safe_locate_with_a_missing_file_returns_none(loaded, caplog):
    doc = Document.objects.filter(organization=loaded).first()
    doc.file.storage.delete(doc.file.name)
    assert L.safe_locate(doc) is None
    assert "Could not locate" in caplog.text


@pytest.mark.django_db
def test_correction_relocates_the_new_value(client, user, loaded):
    doc = Document.objects.get(organization=loaded, original_filename="S03_3_freight_invoice.pdf")
    before = doc.fields.get(name="total_amount").location["boxes"]
    line_amount = doc.field("line_items")[0]["amount"]
    client.force_login(user)
    client.post(reverse("review:update_field", args=[doc.pk]), {"name": "total_amount", "value": line_amount})
    total = doc.fields.get(name="total_amount")
    assert total.source == "human" and total.location["status"] == L.FOUND
    assert total.location["boxes"] != before   # now the line where that amount is printed
    assert L.to_decimal(box_text(doc.file.open("rb").read(), total.page, total.location["boxes"][0])) == Decimal(line_amount)

    # A typed value that isn't printed gets no box, and the page says so.
    client.post(reverse("review:update_field", args=[doc.pk]), {"name": "total_amount", "value": "12345.67"})
    total.refresh_from_db()
    assert total.location["status"] == L.NOT_FOUND and total.page is None and "boxes" not in total.location
    entry = doc_payload(doc)["fields"]["total_amount"]
    assert entry == {"label": "total amount", "status": "not_found", "human": True}


@pytest.mark.django_db
def test_locate_fields_command_backfills(loaded, capsys):
    ExtractedField.objects.update(location=None, page=None)
    call_command("locate_fields")
    assert not ExtractedField.objects.filter(location=None).exists()
    assert "values located" in capsys.readouterr().out
    call_command("locate_fields", "--all", "--org", loaded.slug)
    with pytest.raises(CommandError):
        call_command("locate_fields", "--org", "no-such-org")


# --------------------------------------------------------------------------- review screens


def _payload(html: str, doc_id: int) -> dict:
    m = re.search(rf'<script id="evidence-doc-{doc_id}" type="application/json">(.*?)</script>', html, re.S)
    assert m, f"no evidence data for document {doc_id}"
    return json.loads(m.group(1))


@pytest.fixture
def mismatch(org, dataset):
    names = ["S03_1_commercial_invoice.pdf", "S03_2_bill_of_lading.pdf", "S03_3_freight_invoice.pdf"]
    truth = {d["file"]: d for d in ground_truth(dataset)}
    for name in names:
        ingest_bytes(org, name, (Path(dataset) / "pdf" / name).read_bytes(), process="sync")
    assert "total_mismatch" in truth["S03_3_freight_invoice.pdf"]["planted_errors"]
    return Shipment.objects.get(organization=org, issues__code="total_mismatch")


@pytest.mark.django_db
def test_shipment_page_renders_evidence_hooks(client, user, mismatch):
    client.force_login(user)
    html = client.get(reverse("review:shipment", args=[mismatch.pk])).content.decode()
    freight = Document.objects.get(original_filename="S03_3_freight_invoice.pdf")
    assert 'data-evidence-field="total_amount"' in html
    assert 'data-evidence-line="0"' in html
    assert "data-evidence-viewer" in html and "vendor/pdfjs/pdf.min.mjs" in html and "vendor/pdfjs/pdf.worker.min.mjs" in html
    assert "js/evidence.js" in html
    assert (f'data-evidence-issue data-evidence-doc="{freight.pk}" '
            'data-evidence-targets="total_amount line_items.amount"') in html
    data = _payload(html, freight.pk)
    assert data["mode"] == "pdfjs" and data["url"] == reverse("review:document_file", args=[freight.pk])
    total = data["fields"]["total_amount"]
    assert total["status"] == "found" and total["hits"][0]["p"] == 1 and len(total["hits"][0]["b"][0]) == 4
    lines = data["fields"]["line_items"]["items"]
    assert set(lines) == {str(i) for i in range(len(freight.field("line_items")))}
    assert all(item["a"] for item in lines.values())
    assert data["fields"]["container_numbers"]["items"]


@pytest.mark.django_db
def test_viewer_role_sees_evidence_but_other_orgs_do_not(client, viewer, mismatch):
    client.force_login(viewer)
    r = client.get(reverse("review:shipment", args=[mismatch.pk]))
    assert r.status_code == 200 and b'data-evidence-field="total_amount"' in r.content

    from django.contrib.auth import get_user_model

    other_org = Organization.objects.create(name="Other Co", slug="other")
    outsider = get_user_model().objects.create_user("outsider", password="pw-123456789-test")
    Membership.objects.create(user=outsider, organization=other_org, role=Membership.Role.ADMIN)
    client.force_login(outsider)
    doc = mismatch.documents.first()
    assert client.get(reverse("review:shipment", args=[mismatch.pk])).status_code == 404
    assert client.get(reverse("review:document_file", args=[doc.pk])).status_code == 404
    client.logout()
    assert client.get(reverse("review:document_file", args=[doc.pk])).status_code == 302


@pytest.mark.django_db
def test_document_page_for_a_scan_says_location_is_not_available(client, user, org, dataset):
    d = next(x for x in ground_truth(dataset) if x["scanned"])
    doc, _ = ingest_bytes(org, d["file"], (Path(dataset) / "pdf" / d["file"]).read_bytes(), process="sync")
    assert doc.status == Document.Status.NEEDS_OCR
    client.force_login(user)  # the reviewer types the B/L number from the image
    client.post(reverse("review:update_field", args=[doc.pk]), {"name": "bl_number", "value": "ZZZZ9999999"})
    doc.refresh_from_db()
    url = (reverse("review:document", args=[doc.pk]) if doc.status != Document.Status.MATCHED
           else reverse("review:shipment", args=[doc.match.shipment_id]))
    html = client.get(url).content.decode()
    data = _payload(html, doc.pk)
    assert data["mode"] == "scanned"
    assert data["fields"]["bl_number"]["status"] == "scanned"
    assert 'data-evidence-field="bl_number"' in html


@pytest.mark.django_db
def test_document_page_renders_hooks_for_an_unmatched_text_document(client, user, org):
    data = make_pdf([(50, 60, "Bayside Customs Brokerage Inc.", "Helvetica-Bold", 13),
                     (50, 100, "FREIGHT INVOICE"), (50, 120, "Invoice No.: BCB-77120"), (50, 136, "Date: 4 Sep 2026"),
                     (50, 170, "Customs entry filing"), (480, 170, "1,250.00"), (350, 200, "Total due:"), (480, 200, "1,250.00")])
    doc, _ = ingest_bytes(org, "bayside.pdf", data, process="sync")
    assert doc.status == Document.Status.UNMATCHED
    client.force_login(user)
    html = client.get(reverse("review:document", args=[doc.pk])).content.decode()
    payload = _payload(html, doc.pk)
    assert payload["mode"] == "pdfjs"
    assert payload["fields"]["invoice_number"]["hits"][0]["p"] == 1
    assert 'data-evidence-field="invoice_number"' in html and 'data-evidence-field="bl_number"' in html
    assert "data-evidence-viewer" in html


# --------------------------------------------------------------------------- issue targets, vendored files


@pytest.mark.parametrize("code,data,expected", [
    ("total_mismatch", {}, ["total_amount", "line_items.amount"]),
    ("invalid_container", {"key": "MSCU1234565"}, ["container_numbers=MSCU1234565"]),
    ("container_not_on_bl", {"key": "TGHU7654321"}, ["container_numbers=TGHU7654321"]),
    ("duplicate_invoice", {"duplicate_of": 3}, ["invoice_number"]),
    ("low_confidence", {"fields": ["vendor_name", "bl_number"]}, ["vendor_name", "bl_number"]),
    ("missing_field", {"fields": ["total_amount"]}, None),
    ("fuzzy_match", {}, None),
])
def test_issue_targets(code, data, expected):
    issue = ValidationIssue(code=code, severity="error", message="", data=data, document_id=1)
    found_targets = issue_targets(issue)
    assert (found_targets[0] if found_targets else None) == expected
    if code == "low_confidence":
        assert found_targets[1] == "the vendor name and B/L number"


def test_shipment_level_issue_has_no_target():
    assert issue_targets(ValidationIssue(code="total_mismatch", severity="error", message="", data={})) is None


def test_vendored_pdfjs_is_complete():
    root = Path(settings.BASE_DIR) / "static" / "vendor" / "pdfjs"
    for name in ["pdf.min.mjs", "pdf.worker.min.mjs", "LICENSE", "wasm/jbig2.wasm", "wasm/openjpeg.wasm"]:
        assert (root / name).is_file(), name
    assert "Apache License" in (root / "LICENSE").read_text()
    assert "6.3.289" in (root / "pdf.min.mjs").read_text(errors="ignore")[:2000] or "6.3.289" in (root / "VERSION").read_text()
