"""Claude extraction against a fake Anthropic API: request shape, retries, usage and PDF input."""
import json

import httpx
import pytest

from apps.documents.services import extract as extract_mod
from apps.documents.services import llm

REAL_CLIENT = httpx.Client


class FakeAnthropic:
    def __init__(self, answers, fail_first=None):
        self.answers = list(answers)
        self.fail_first = list(fail_first or [])
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.fail_first:
            status, text = self.fail_first.pop(0)
            return httpx.Response(status, text=text, headers={"retry-after": "0"})
        answer = self.answers.pop(0)
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return httpx.Response(200, json={
            "model": body["model"], "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 2000, "output_tokens": 300},
        })


@pytest.fixture
def claude(settings, monkeypatch):
    settings.EXTRACTION_PROVIDER = "anthropic"
    settings.ANTHROPIC_API_KEY = "test-key"
    settings.LLM_MODEL = ""
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    def install(fake):
        monkeypatch.setattr(llm.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(fake.handler)))
        return fake
    return install


TEXT = """FREIGHT INVOICE
Atlas Freight Partners LLC
Invoice No: AT-946721   Invoice Date: 2026-05-10
B/L No: PCXL5569956444   Container: PCXU2268822
Ocean Freight 4668.02
Terminal Handling Charge (THC) 701.18
TOTAL DUE USD 5369.20
"""

ANSWER = {
    "vendor_name": "Atlas Freight Partners LLC", "invoice_number": "AT-946721", "invoice_date": "2026-05-10",
    "due_date": None, "currency": "USD", "bl_number": "PCXL5569956444", "container_numbers": ["PCXU2268822"],
    "po_numbers": [], "total_amount": 5369.20,
    "line_items": [{"description": "Ocean Freight", "quantity": None, "unit_price": None, "amount": 4668.02},
                   {"description": "Terminal Handling Charge (THC)", "quantity": None, "unit_price": None, "amount": 701.18}],
}


def test_structured_output_request_and_grounding(claude):
    fake = claude(FakeAnthropic([ANSWER]))
    with llm.track_usage() as calls:
        fields, provider = extract_mod.extract("freight_invoice", TEXT)
    body = fake.requests[0]
    schema = body["output_config"]["format"]["schema"]
    assert body["model"] == "claude-sonnet-5-5" and provider == "anthropic"
    assert schema["additionalProperties"] is False and "$ref" not in json.dumps(schema)
    assert schema["properties"]["line_items"]["items"]["additionalProperties"] is False
    by = {f.name: f for f in fields}
    assert by["invoice_number"].grounded and by["invoice_number"].confidence > 0.9
    assert by["total_amount"].value in ("5369.2", "5369.20") and by["total_amount"].grounded
    assert "due_date" not in by  # null means not printed, not an empty value
    usage = llm.summarize(calls)
    assert usage["input_tokens"] == 2000 and usage["cost_usd"] == pytest.approx(0.007)


def test_ungrounded_value_goes_to_review(claude):
    claude(FakeAnthropic([{**ANSWER, "invoice_number": "AT-999999"}]))
    fields, _ = extract_mod.extract("freight_invoice", TEXT)
    inv = next(f for f in fields if f.name == "invoice_number")
    assert not inv.grounded and inv.confidence < 0.85


def test_unparseable_field_is_dropped_not_fatal(claude):
    claude(FakeAnthropic([{**ANSWER, "invoice_date": "next Tuesday"}]))
    fields, provider = extract_mod.extract("freight_invoice", TEXT)
    names = {f.name for f in fields}
    assert provider == "anthropic" and "invoice_date" not in names and "invoice_number" in names


def test_retries_overload_then_succeeds(claude):
    fake = claude(FakeAnthropic([ANSWER], fail_first=[(529, "overloaded"), (429, "rate limited")]))
    with llm.track_usage() as calls:
        extract_mod.extract("freight_invoice", TEXT)
    assert len(fake.requests) == 3 and calls[0].attempts == 3


def test_temperature_rejected_is_retried_without_it(claude):
    fake = claude(FakeAnthropic([ANSWER], fail_first=[(400, '{"error":{"message":"temperature is not supported"}}')]))
    extract_mod.extract("freight_invoice", TEXT)
    assert "temperature" in fake.requests[0] and "temperature" not in fake.requests[1]


def test_pdf_is_sent_as_document_block(claude):
    fake = claude(FakeAnthropic([ANSWER]))
    extract_mod.extract("freight_invoice", TEXT, pdf=b"%PDF-1.4 fake")
    content = fake.requests[0]["messages"][0]["content"]
    assert content[0]["type"] == "document" and content[0]["source"]["media_type"] == "application/pdf"


def test_api_error_falls_back_to_rules(claude):
    claude(FakeAnthropic([], fail_first=[(401, '{"error":{"type":"authentication_error","message":"invalid x-api-key"}}')]))
    fields, provider = extract_mod.extract("freight_invoice", TEXT)
    assert provider == "rules" and any(f.name == "invoice_number" for f in fields)


@pytest.mark.django_db
def test_scanned_pdf_is_read_by_claude(claude, settings, org, dataset):
    """A scan with no text layer: Claude transcribes it, then extraction and grounding run on that text."""
    import json as _json

    from apps.documents.models import Document
    from apps.documents.services.ingest import ingest_bytes

    settings.OCR_PROVIDER = "auto"
    truth = _json.loads((dataset / "ground_truth.json").read_text())
    scanned = next(d for d in truth["documents"] if d["scanned"])
    f = scanned["fields"]
    transcription = "\n".join(f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in f.items()
                              if not isinstance(v, (dict,)) and k != "line_items")
    answer = {k: (v if k != "line_items" else []) for k, v in f.items() if k != "layout_variant"}
    claude(FakeAnthropic([transcription, *[answer] * 3]))
    doc, _ = ingest_bytes(org, scanned["file"], (dataset / "pdf" / scanned["file"]).read_bytes(), process="sync")
    doc.refresh_from_db()
    assert doc.text_source == "anthropic" and doc.status != Document.Status.NEEDS_OCR
    assert doc.llm_usage["calls"][0]["purpose"] == "ocr" and doc.llm_usage["cost_usd"] > 0
