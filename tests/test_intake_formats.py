"""Intake of photos, scans and spreadsheets: conversion to a PDF copy, OCR, rules extraction and every intake path."""
import base64
import io
import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse
from PIL import Image
from pypdf import PdfReader

from apps.documents.models import Document
from apps.documents.services import llm
from apps.documents.services.ingest import RejectedFile, ingest_bytes
from apps.documents.services.ocr import read_text
from apps.intake.services import sheets
from apps.intake.services.formats import detect, is_supported_name
from synthetic import extra


def _truth(dataset, shipment="S01"):
    gt = json.loads((dataset / "ground_truth.json").read_text())
    return next(s for s in gt["shipments"] if s["shipment_id"] == shipment)


def _pdf_pages(doc) -> list:
    with doc.file.open("rb") as fh:
        return PdfReader(io.BytesIO(fh.read())).pages


@pytest.fixture
def ocr_by_claude(settings, monkeypatch):
    """Claude OCR without calling Anthropic: the 'transcription' is the text layer of the source PDF."""
    settings.OCR_PROVIDER = "anthropic"
    settings.ANTHROPIC_API_KEY = "test-key"
    texts, calls = [], []

    def transcribe(pdf, client=None, timeout=180.0):
        calls.append(len(pdf))
        return texts.pop(0)
    monkeypatch.setattr(llm, "transcribe_pdf", transcribe)
    return texts, calls


# ---------------------------------------------------------------- recognizing files


@pytest.mark.parametrize("name,content,kind", [
    ("a.pdf", b"%PDF-1.4 x", "pdf"),
    ("IMG.JPG", b"\xff\xd8\xff\xe0rest", "image"),
    ("scan.png", b"\x89PNG\r\n\x1a\nrest", "image"),
    ("fax.tif", b"II*\x00rest", "image"),
    ("p.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image"),
    ("export.csv", b"a,b\n1,2\n", "spreadsheet"),
])
def test_detects_by_content(name, content, kind):
    assert detect(name, content).kind == kind


def test_content_wins_over_the_name():
    png = extra.photo(extra.credit_note("CN-1", "INV-1")[0], "PNG")
    assert detect("looks-like.pdf", png).kind == "image"


@pytest.mark.parametrize("name,content,words", [
    ("old.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, "Save As > Excel Workbook (.xlsx)"),
    ("IMG_0001.HEIC", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32, "HEIC photos"),
    ("letter.docx", extra.zip_of({"word/document.xml": b"<w/>", "[Content_Types].xml": b"<x/>"}), "Word documents"),
    ("notes.txt", b"hello", "not a PDF, image, spreadsheet or ZIP file"),
    ("empty.pdf", b"", "the file is empty"),
    ("pack.rar", b"Rar!\x1a\x07\x00rest", "Send a ZIP file instead"),
])
def test_unsupported_files_say_what_to_do(name, content, words):
    with pytest.raises(RejectedFile) as e:
        detect(name, content)
    assert words in str(e.value) and str(e.value).startswith(name)


def test_supported_names_for_folder_and_mail_scans():
    assert is_supported_name("Invoice.PDF") and is_supported_name("photo.jpeg") and is_supported_name("x.xlsx")
    assert not is_supported_name(".hidden.pdf") and not is_supported_name("Thumbs.db")
    assert not is_supported_name("notes.docx")


# ---------------------------------------------------------------- images


@pytest.mark.django_db
def test_photo_becomes_pdf_and_keeps_original(org, dataset):
    png = extra.photo((dataset / "pdf" / "S01_3_freight_invoice.pdf").read_bytes(), "PNG")
    doc, created = ingest_bytes(org, "IMG_2041.png", png, process="sync")
    assert created and doc.source_format == "image"
    assert doc.original_filename == "IMG_2041.png" and doc.pdf_filename == "IMG_2041.pdf"
    with doc.original_file.open("rb") as fh:
        assert fh.read() == png
    assert len(_pdf_pages(doc)) == 1
    # No OCR configured: the photo waits for a person, with the usual needs-OCR status.
    assert doc.status == Document.Status.NEEDS_OCR and doc.text_source == "none"
    assert doc.intake["image"]["source"] == "PNG"


@pytest.mark.django_db
def test_photo_is_read_by_ocr_and_matched(org, dataset, ocr_by_claude):
    texts, calls = ocr_by_claude
    source = (dataset / "pdf" / "S01_3_freight_invoice.pdf").read_bytes()
    texts.append(read_text(source).text)
    ingest_bytes(org, "S01_2_bill_of_lading.pdf", (dataset / "pdf" / "S01_2_bill_of_lading.pdf").read_bytes(),
                 process="sync")
    doc, _ = ingest_bytes(org, "photo.jpg", extra.photo(source, "JPEG"), process="sync")
    assert calls and doc.text_source == "anthropic"
    assert doc.doc_type == "freight_invoice" and doc.status == Document.Status.MATCHED
    assert doc.field("bl_number") == _truth(dataset)["bl_number"]


@pytest.mark.django_db
def test_exif_orientation_is_applied(org):
    pdf, _ = extra.credit_note("CN-7", "INV-7")
    jpg = extra.photo(pdf, "JPEG", sideways=True)
    assert Image.open(io.BytesIO(jpg)).size[0] > Image.open(io.BytesIO(jpg)).size[1]  # stored on its side
    doc, _ = ingest_bytes(org, "sideways.jpg", jpg, process="none")
    page = _pdf_pages(doc)[0]
    assert float(page.mediabox.height) > float(page.mediabox.width)  # upright in the PDF
    assert doc.intake["image"]["rotated"] is True


@pytest.mark.django_db
def test_multipage_tiff_becomes_one_page_per_frame(org):
    pdfs = [extra.credit_note(f"CN-{n}", f"INV-{n}")[0] for n in range(3)]
    doc, _ = ingest_bytes(org, "scan.tiff", extra.tiff_scan(pdfs), process="sync")
    assert len(_pdf_pages(doc)) == 3 and doc.page_count == 3
    assert doc.intake["image"]["pages"] == 3


@pytest.mark.django_db
def test_webp_is_accepted(org):
    img = extra.page_image(extra.credit_note("CN-8", "INV-8")[0])
    out = io.BytesIO()
    img.save(out, format="WEBP")
    doc, created = ingest_bytes(org, "shot.webp", out.getvalue(), process="none")
    assert created and doc.source_format == "image" and doc.intake["subtype"] == "webp"


@pytest.mark.django_db
def test_bad_images_are_refused(org, settings):
    logo = io.BytesIO()
    Image.new("RGB", (180, 60), "navy").save(logo, format="PNG")
    with pytest.raises(RejectedFile, match="too small to be a document page"):
        ingest_bytes(org, "image001.png", logo.getvalue())
    with pytest.raises(RejectedFile, match="can't be opened"):
        ingest_bytes(org, "broken.png", b"\x89PNG\r\n\x1a\n" + b"garbage" * 50)
    settings.INTAKE_MAX_IMAGE_MEGAPIXELS = 1
    big = io.BytesIO()
    Image.new("L", (1500, 1000), 255).save(big, format="PNG")
    with pytest.raises(RejectedFile, match="too large"):
        ingest_bytes(org, "huge.png", big.getvalue())
    assert not Document.objects.filter(organization=org).exists()


# ---------------------------------------------------------------- spreadsheets


@pytest.mark.django_db
def test_xlsx_invoice_extracts_and_matches_without_ai(org, dataset):
    truth = _truth(dataset)
    ingest_bytes(org, "S01_2_bill_of_lading.pdf", (dataset / "pdf" / "S01_2_bill_of_lading.pdf").read_bytes(),
                 process="sync")
    xlsx, expected = extra.invoice_xlsx(truth["bl_number"], truth["container_numbers"])
    doc, _ = ingest_bytes(org, "Harborlink March.xlsx", xlsx, process="sync")
    assert doc.source_format == "spreadsheet" and doc.text_source == "spreadsheet"
    assert doc.doc_type == "freight_invoice" and doc.extraction_provider == "rules"
    data = doc.data()
    for name in ("vendor_name", "invoice_number", "invoice_date", "due_date", "bl_number", "currency", "total_amount",
                 "po_numbers", "line_items"):
        assert data[name] == expected[name], name
    assert set(data["container_numbers"]) == set(expected["container_numbers"])
    assert all(f.grounded and f.confidence >= 0.9 for f in doc.fields.all())
    assert doc.status == Document.Status.MATCHED and doc.match.method == "exact_bl"
    # A readable PDF copy for the review screen; the workbook itself for download.
    with doc.file.open("rb") as fh:
        assert fh.read(5) == b"%PDF-"
    assert "Ocean Freight | 2 | 2,150.00 | 4,300.00" in doc.text
    assert not doc.issues.filter(severity="error").exists()


@pytest.mark.django_db
def test_csv_export_with_references_on_every_row(org):
    csv_bytes, expected = extra.invoice_csv("PCXL5569956444", "PCXU2268822")
    doc, _ = ingest_bytes(org, "atlas-export.csv", csv_bytes, process="sync")
    data = doc.data()
    assert doc.doc_type == "freight_invoice"
    for name in ("vendor_name", "invoice_number", "invoice_date", "bl_number", "container_numbers", "currency",
                 "total_amount", "line_items"):
        assert data[name] == expected[name], name


@pytest.mark.django_db
def test_spreadsheet_is_not_sent_as_pdf_to_ai(org, settings, monkeypatch):
    settings.EXTRACTION_PROVIDER, settings.ANTHROPIC_API_KEY, settings.LLM_INPUT = "anthropic", "k", "auto"
    seen = []

    def fake_call(system, user, schema, name="record", timeout=120.0, client=None, pdf=None, purpose="extract"):
        seen.append((purpose, pdf))
        raise llm.LLMError("offline")  # falls back to the spreadsheet reader
    monkeypatch.setattr(llm, "structured_call", fake_call)
    xlsx, expected = extra.invoice_xlsx("OSLN1234567890", ["OSLU1234565"])
    doc, _ = ingest_bytes(org, "inv.xlsx", xlsx, process="sync")
    assert seen and all(pdf is None for _, pdf in seen)
    assert doc.field("invoice_number") == expected["invoice_number"]


def test_header_row_and_labels():
    rows = [["Acme Freight Ltd"], [], ["Cntr No.", "Charge Description", "Qty", "Unit Rate", "Amount (EUR)"],
            ["MSCU1234565", "THC", "1", "300.00", "300.00"]]
    h, cols = sheets.find_header(rows)
    assert h == 2 and cols == {"container_numbers": 0, "description": 1, "quantity": 2, "unit_price": 3, "amount": 4}
    assert sheets.norm_label("Amount (USD):") == ("amount", "USD")
    assert sheets._is_total_label("TOTAL DUE") == "total" and sheets._is_total_label("Sub-total") == "subtotal"
    assert sheets._is_total_label("Total Logistics Fee") is None


def test_credit_note_spreadsheet():
    rows = [["Harborlink Logistics LLC", "", "", "CREDIT NOTE"], ["Credit Note No.", "CN-4410", "", "Date", "2026-05-02"],
            ["Original Invoice", "HA-123456", "", "Currency", "USD"], [],
            ["Description", "Amount"], ["THC overcharge", "-120.00"], ["Documentation fee", "(65.00)"],
            ["Total credit", "-185.00"]]
    text = sheets.to_text("cn.xlsx", [sheets.Sheet("CN", rows)])
    r = sheets.sheet_rules("credit_note", text)
    assert r.values["credit_note_number"] == "CN-4410" and r.values["original_invoice_number"] == "HA-123456"
    assert r.values["total_amount"] == "185.00" and r.values["invoice_date"] == "2026-05-02"
    assert [i["amount"] for i in r.values["line_items"]] == ["120.00", "65.00"]
    assert r.values["vendor_name"] == "Harborlink Logistics LLC" and r.values["currency"] == "USD"


def test_text_form_round_trips_cells():
    sheet = sheets.Sheet("S", [["Ref | note", "", "x"], [], ["a"]])
    text = sheets.to_text("f.xlsx", [sheet])
    assert sheets.is_sheet_text(text)
    assert sheets.parse_text(text) == [[["Ref | note", "", "x"], [], ["a"]]]


@pytest.mark.django_db
def test_bad_spreadsheets_are_refused(org):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active["A1"] = "secret"
    wb.active.sheet_state = "visible"
    hidden = wb.create_sheet("Hidden")
    hidden["A1"] = "x"
    hidden.sheet_state = "hidden"
    wb.active["A1"] = None
    out = io.BytesIO()
    wb.save(out)
    with pytest.raises(RejectedFile, match="the spreadsheet is empty"):
        ingest_bytes(org, "blank.xlsx", out.getvalue())
    with pytest.raises(RejectedFile, match="older Excel files"):
        ingest_bytes(org, "old.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)


# ---------------------------------------------------------------- every intake path


@pytest.mark.django_db
def test_upload_form_accepts_new_types(client, user, org, dataset):
    client.force_login(user)
    page = client.get(reverse("review:documents")).content.decode()
    assert ".xlsx" in page and "image/jpeg" in page and ".zip" in page
    png = extra.photo((dataset / "pdf" / "S01_3_freight_invoice.pdf").read_bytes(), "PNG")
    xlsx, _ = extra.invoice_xlsx("OSLN1234567890", ["OSLU1234565"])
    r = client.post(reverse("review:upload"), {"files": [SimpleUploadedFile("IMG_1.png", png, "image/png"),
                                                         SimpleUploadedFile("rates.xlsx", xlsx)]}, follow=True)
    text = r.content.decode()
    assert "Received 2 documents: IMG_1.png, rates.xlsx" in text
    assert "is a photo" in text  # OCR is off in tests, so the reviewer is told to type the key numbers
    assert Document.objects.filter(organization=org, source_format="image").count() == 1


@pytest.mark.django_db
def test_api_upload_and_original_download(client, user, viewer, org, dataset, django_user_model):
    client.force_login(user)
    jpg = extra.photo((dataset / "pdf" / "S01_3_freight_invoice.pdf").read_bytes(), "JPEG")
    r = client.post(f"/api/{org.slug}/documents", {"file": SimpleUploadedFile("IMG_9.jpg", jpg, "image/jpeg")})
    assert r.status_code == 201 and r.json()["source_format"] == "image"
    doc = Document.objects.get(pk=r.json()["id"])

    client.force_login(viewer)  # viewers may download what they can see
    r = client.get(reverse("intake:original", args=[doc.pk]))
    assert r.status_code == 200 and b"".join(r.streaming_content) == jpg
    assert r["Content-Type"] == "image/jpeg" and 'attachment; filename="IMG_9.jpg"' in r["Content-Disposition"]
    r = client.get(reverse("review:document_file", args=[doc.pk]))
    assert r["Content-Type"] == "application/pdf" and "IMG_9.pdf" in r["Content-Disposition"]

    client.force_login(django_user_model.objects.create_user("outsider", password="pw-123456789-x"))
    assert client.get(reverse("intake:original", args=[doc.pk])).status_code == 404


@pytest.mark.django_db
def test_document_page_shows_conversion(client, user, org):
    pdf, _ = extra.credit_note("CN-11", "INV-11")
    doc, _ = ingest_bytes(org, "IMG_77.png", extra.photo(pdf, "PNG"), process="sync")
    client.force_login(user)
    page = client.get(reverse("review:document", args=[doc.pk])).content.decode()
    assert "Original file: PNG image" in page and reverse("intake:original", args=[doc.pk]) in page


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeGmail:
    """Just enough of the Gmail API client for poll()."""

    def __init__(self, messages):
        self._messages, self.queries = messages, []

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return self

    def list(self, userId, q, maxResults):
        self.queries.append(q)
        return _Exec({"messages": [{"id": k} for k in self._messages]})

    def get(self, userId, id, format=None, messageId=None):
        return _Exec(self._messages[id])


def _part(filename, data, mime="application/octet-stream", headers=()):
    return {"filename": filename, "mimeType": mime, "headers": list(headers),
            "body": {"data": base64.urlsafe_b64encode(data).decode()}}


@pytest.mark.django_db
def test_gmail_imports_photos_spreadsheets_and_zips_but_not_logos(org, dataset):
    from apps.documents.services import gmail

    source = (dataset / "pdf" / "S01_3_freight_invoice.pdf").read_bytes()
    logo = io.BytesIO()
    Image.new("RGB", (900, 300), "white").save(logo, format="PNG")  # big enough to pass as a page, but inline
    xlsx, _ = extra.invoice_xlsx("OSLN1234567890", ["OSLU1234565"])
    message = {"internalDate": "1790000000000", "payload": {
        "headers": [{"name": "Subject", "value": "Invoices"}, {"name": "From", "value": "billing@harborlink.example"}],
        "parts": [
            _part("", b"Please find attached.", "text/plain"),
            _part("image001.png", logo.getvalue(), "image/png",
                  [{"name": "Content-ID", "value": "<image001>"}, {"name": "Content-Disposition", "value": "inline"}]),
            _part("invoice.pdf", source, "application/pdf"),
            _part("photo.jpg", extra.photo(source, "JPEG"), "image/jpeg"),
            _part("rates.xlsx", xlsx),
            _part("bundle.zip", extra.zip_of({"cn.pdf": extra.credit_note("CN-5", "INV-5")[0]})),
            _part("terms.docx", b"PK\x03\x04"),
        ]}}
    service = FakeGmail({"m1": message})
    stats = gmail.poll(org, service=service, process="none")
    assert "filename:xlsx" in service.queries[0] and "filename:zip" in service.queries[0]
    assert stats["documents"] == 4 and stats["unsupported"] == 1
    names = set(Document.objects.filter(organization=org).values_list("original_filename", flat=True))
    assert names == {"invoice.pdf", "photo.jpg", "rates.xlsx", "bundle.zip", "cn.pdf"}
    assert Document.objects.get(original_filename="cn.pdf").email.subject == "Invoices"


@pytest.mark.django_db
def test_ingest_folder_takes_every_supported_file(org, tmp_path, dataset):
    folder = tmp_path / "inbox"
    folder.mkdir()
    source = (dataset / "pdf" / "S01_1_commercial_invoice.pdf").read_bytes()
    (folder / "a.pdf").write_bytes(source)
    (folder / "b.png").write_bytes(extra.photo(source, "PNG"))
    (folder / "c.xlsx").write_bytes(extra.invoice_xlsx("OSLN1234567890", ["OSLU1234565"])[0])
    (folder / "d.zip").write_bytes(extra.zip_of({"cn.pdf": extra.credit_note("CN-6", "INV-6")[0]}))
    (folder / ".hidden.pdf").write_bytes(b"%PDF-1.4")
    (folder / "notes.txt").write_text("not a document")
    out = io.StringIO()
    call_command("ingest_folder", str(folder), org=org.slug, stdout=out, stderr=io.StringIO())
    assert "Imported 4" in out.getvalue()
    assert Document.objects.filter(organization=org).count() == 5  # four files + the PDF inside the ZIP
