import json

import pytest

from apps.documents.services import extract as extract_mod
from apps.documents.services.classify import classify
from apps.documents.services.extract import extract
from apps.documents.services.ocr import read_text


def _docs(dataset):
    return json.loads((dataset / "ground_truth.json").read_text())["documents"]


def test_rules_extract_every_ground_truth_field(dataset):
    checked = 0
    for gt in _docs(dataset):
        if gt["scanned"]:
            continue
        tr = read_text((dataset / "pdf" / gt["file"]).read_bytes())
        doc_type, _ = classify(tr.text)
        assert doc_type == gt["doc_type"], gt["file"]
        fields = {f.name: f.value for f in extract(doc_type, tr.text)[0]}
        for name in ("invoice_number", "bl_number", "total_amount", "invoice_date"):
            if name in gt["fields"]:
                assert str(fields.get(name)) == str(gt["fields"][name]), (gt["file"], name)
                checked += 1
        assert sorted(fields.get("container_numbers", [])) == sorted(gt["fields"]["container_numbers"])
    assert checked > 30


def test_scanned_pdf_is_flagged_for_ocr(dataset):
    scanned = next(d for d in _docs(dataset) if d["scanned"])
    assert read_text((dataset / "pdf" / scanned["file"]).read_bytes()).needs_ocr


def _pdf(pages: int, line: str, image: bytes | None = None) -> bytes:
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4)
    for _ in range(pages):
        if image:
            c.drawImage(ImageReader(io.BytesIO(image)), 0, 0, *A4)
        c.drawString(72, 800, line)
        c.showPage()
    c.save()
    return out.getvalue()


def test_sparse_typed_pdf_is_read_from_its_text_layer():
    """QA-073: a short line of real text per page is not a scan; OCR would find nothing more."""
    tr = read_text(_pdf(40, "Packing slip continued"))
    assert not tr.needs_ocr and tr.method == "text_layer" and tr.page_count == 40
    assert tr.text.count("Packing slip continued") == 40


def test_scanned_page_with_a_short_stamped_line_still_needs_ocr(dataset):
    import io

    import pdfplumber

    scanned = next(d for d in _docs(dataset) if d["scanned"])
    with pdfplumber.open(dataset / "pdf" / scanned["file"]) as pdf:
        png = io.BytesIO()
        pdf.pages[0].to_image(resolution=40).original.save(png, format="PNG")
    assert read_text(_pdf(2, "Received 2026-10-01", image=png.getvalue())).needs_ocr


def test_llm_value_not_in_text_gets_low_confidence(settings, monkeypatch):
    """Grounding: a value the document does not contain is treated as a possible hallucination."""
    settings.EXTRACTION_PROVIDER = "anthropic"
    text = "Harborlink Logistics LLC\nInvoice No.: HA-1001\nB/L No.: OSLN1234567890\nTotal: 2,500.00"
    monkeypatch.setattr(extract_mod.llm, "structured_call", lambda *a, **k: {
        "vendor_name": "Harborlink Logistics LLC", "invoice_number": "HA-1001",
        "bl_number": "OSLN1234567890", "total_amount": "2600.00",  # wrong total, not in the text
    })
    fields, provider = extract("freight_invoice", text)
    by_name = {f.name: f for f in fields}
    assert provider == "anthropic"
    assert by_name["invoice_number"].grounded and by_name["invoice_number"].confidence == pytest.approx(0.95)
    assert not by_name["total_amount"].grounded and by_name["total_amount"].confidence == pytest.approx(0.5)


def test_llm_failure_falls_back_to_rules(settings, monkeypatch):
    settings.EXTRACTION_PROVIDER = "openai"

    def boom(*a, **k):
        raise extract_mod.llm.LLMError("down")

    monkeypatch.setattr(extract_mod.llm, "structured_call", boom)
    fields, provider = extract("freight_invoice", "Acme Freight LLC\nInvoice No.: X-1\nTotal: 10.00")
    assert provider == "rules"
    assert {f.name for f in fields} >= {"invoice_number", "total_amount"}
