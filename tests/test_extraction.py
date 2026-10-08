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
