"""Credit notes: classification, reading, matching to the invoice they credit, checks, totals and QuickBooks."""
import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounting.models import PostedBill, QBOConnection
from apps.accounting.services import quickbooks
from apps.accounting.services.posting import post_shipment
from apps.accounting.services.quickbooks import QBOClient
from apps.documents.models import Document
from apps.documents.schemas import wire_schema
from apps.documents.services import llm
from apps.documents.services.classify import classify
from apps.documents.services.extract import extract
from apps.documents.services.ingest import ingest_bytes
from apps.documents.services.ocr import read_text
from apps.shipments.models import Shipment
from apps.shipments.services.approval import shipment_totals
from synthetic import extra
from synthetic.generator import FORWARDERS
from tests.test_quickbooks import FakeQBO, fault


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(quickbooks.time, "sleep", lambda s: None)


@pytest.fixture
def shipment(org, dataset):
    """S01 with its commercial invoice, B/L and freight invoice."""
    for f in ["S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.pdf", "S01_3_freight_invoice.pdf"]:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    return Shipment.objects.get(organization=org)


def _freight(shipment):
    return next(d for d in shipment.documents if d.doc_type == "freight_invoice")


def _vendor(doc):
    return next(p for p in FORWARDERS if p.name == doc.field("vendor_name"))


@pytest.mark.parametrize("style", ["minus", "brackets", "plain"])
def test_credit_note_is_classified_and_read(style):
    pdf, truth = extra.credit_note("CN-100245", "HA-123456", bl="OSLN1234567890", containers=["OSLU1234565"],
                                   style=style)
    text = read_text(pdf).text
    assert classify(text)[0] == "credit_note"
    fields, provider = extract("credit_note", text)
    got = {f.name: f for f in fields}
    for name in ("vendor_name", "credit_note_number", "original_invoice_number", "invoice_date", "currency",
                 "total_amount", "line_items"):
        assert got[name].value == truth[name], name
        assert got[name].grounded and got[name].confidence >= 0.9, name
    assert Decimal(got["total_amount"].value) > 0  # always stored as a positive credit


def test_credit_note_titles_win_over_invoice_words():
    text = "Harborlink Logistics LLC\nCREDIT NOTE for freight charges\nCredit Note No.: CN-1\nOcean freight -100.00"
    assert classify(text)[0] == "credit_note"


def test_ai_reading_of_a_credit_note(settings, monkeypatch):
    settings.EXTRACTION_PROVIDER, settings.ANTHROPIC_API_KEY = "anthropic", "k"
    pdf, truth = extra.credit_note("CN-9", "HA-777777", style="minus")
    text = read_text(pdf).text
    seen = {}

    def fake_call(system, user, schema, name="record", timeout=120.0, client=None, pdf=None, purpose="extract"):
        seen["schema"] = schema
        return {"vendor_name": truth["vendor_name"], "credit_note_number": "CN-9", "original_invoice_number": "HA-777777",
                "invoice_date": "2026-04-20", "currency": "USD", "total_amount": -185.0, "bl_number": None,
                "container_numbers": [], "po_numbers": [],
                "line_items": [{"description": "Terminal Handling Charge (THC) overcharge", "quantity": None,
                                "unit_price": None, "amount": -120.0},
                               {"description": "Documentation Fee refund", "quantity": None, "unit_price": None,
                                "amount": -65.0}]}
    monkeypatch.setattr(llm, "structured_call", fake_call)
    fields, provider = extract("credit_note", text)
    got = {f.name: f for f in fields}
    assert provider == "anthropic" and got["total_amount"].value == "185.00" and got["total_amount"].grounded
    assert [i["amount"] for i in got["line_items"].value] == ["120.00", "65.00"]
    assert seen["schema"] == wire_schema("credit_note") and seen["schema"]["additionalProperties"] is False
    assert {"credit_note_number", "original_invoice_number"} <= set(seen["schema"]["required"])


def test_ai_classifier_knows_credit_notes(settings, monkeypatch):
    from apps.documents.services import classify as classify_mod

    settings.EXTRACTION_PROVIDER, settings.ANTHROPIC_API_KEY = "anthropic", "k"
    seen = {}

    def fake_call(system, user, schema, **kw):
        seen["enum"], seen["user"] = schema["properties"]["doc_type"]["enum"], user
        return {"doc_type": "credit_note"}
    monkeypatch.setattr(llm, "structured_call", fake_call)
    assert classify_mod.classify("a page with nothing recognizable on it at all")[0] == "credit_note"
    assert "credit_note" in seen["enum"] and "credit_note (credit note or credit memo" in seen["user"]


@pytest.mark.django_db
def test_matched_by_original_invoice_and_netted(org, shipment):
    invoice = _freight(shipment)
    before = shipment_totals(shipment)
    pdf, truth = extra.credit_note("CN-500", invoice.field("invoice_number"), vendor=_vendor(invoice))
    cn, _ = ingest_bytes(org, "credit.pdf", pdf, process="sync")
    assert cn.doc_type == "credit_note" and cn.status == Document.Status.MATCHED
    assert cn.match.shipment_id == shipment.pk and cn.match.method == "original_invoice"
    assert "credits invoice" in cn.match.reason
    totals = shipment_totals(shipment)
    assert totals.credits == {"USD": Decimal("185.00")}
    assert totals.by_currency["USD"] == before.by_currency["USD"] - Decimal("185.00")
    codes = set(shipment.issues.filter(resolved=False).values_list("code", flat=True))
    assert not codes & {"duplicate_invoice", "credit_original_not_found", "credit_no_original"}


@pytest.mark.django_db
def test_original_invoice_not_found_and_duplicates(org, shipment):
    invoice = _freight(shipment)
    bl = shipment.bl_number
    pdf, _ = extra.credit_note("CN-600", "ZZ-000001", vendor=_vendor(invoice), bl=bl)
    cn, _ = ingest_bytes(org, "cn-unknown.pdf", pdf, process="sync")
    assert cn.match.shipment_id == shipment.pk and cn.match.method == "exact_bl"
    issues = {i.code: i for i in cn.issues.all()}
    assert issues["credit_original_not_found"].severity == "warning"
    assert "duplicate_invoice" not in issues

    again, _ = extra.credit_note("CN-600", "ZZ-000001", vendor=_vendor(invoice), bl=bl, style="brackets")
    dup, _ = ingest_bytes(org, "cn-unknown-resent.pdf", again, process="sync")
    assert dup.issues.filter(code="duplicate_credit_note", severity="error").exists()

    big, _ = extra.credit_note("CN-601", invoice.field("invoice_number"), vendor=_vendor(invoice),
                               charges=[("Full refund", "99999.00")])
    over, _ = ingest_bytes(org, "cn-big.pdf", big, process="sync")
    assert over.issues.filter(code="credit_exceeds_invoice").exists()

    none, _ = extra.credit_note("CN-602", None, vendor=_vendor(invoice), bl=bl)
    missing, _ = ingest_bytes(org, "cn-no-ref.pdf", none, process="sync")
    assert missing.issues.filter(code="credit_no_original").exists()


@pytest.mark.django_db
def test_credit_note_without_references_waits_for_a_person(org):
    pdf, _ = extra.credit_note("CN-700", "QQ-1", vendor=FORWARDERS[1])
    cn, _ = ingest_bytes(org, "lonely.pdf", pdf, process="sync")
    assert cn.status == Document.Status.UNMATCHED and not Shipment.objects.filter(organization=org).exists()


class FakeQBOWithCredits(FakeQBO):
    """FakeQBO plus the VendorCredit endpoint, with the same requestid idempotency."""

    def __init__(self, fail_credit_once=False, fail_credit_upload_once=False, **kw):
        super().__init__(**kw)
        self.credits, self.credit_requests, self.attached_to = {}, {}, []
        self.fail_credit_once, self.fail_credit_upload_once = fail_credit_once, fail_credit_upload_once

    def handler(self, request):
        path = request.url.path.split("/v3/company/123/")[-1]
        if path == "vendorcredit":
            self.calls.append((request.method, path, dict(request.url.params)))
            if self.fail_credit_once:
                self.fail_credit_once = False
                return httpx.Response(400, json=fault("6000", "A business validation error has occurred",
                                                      "Amount must be positive"))
            rid = request.url.params["requestid"]
            if rid not in self.credit_requests:
                credit_id = str(700 + len(self.credits))
                self.credits[credit_id] = json.loads(request.content)
                self.credit_requests[rid] = credit_id
            return httpx.Response(200, json={"VendorCredit": {"Id": self.credit_requests[rid]}})
        if path == "upload":
            entity = "VendorCredit" if '"type": "VendorCredit"' in request.content.decode("latin-1") else "Bill"
            if entity == "VendorCredit" and self.fail_credit_upload_once:
                self.fail_credit_upload_once = False
                return httpx.Response(503, text="Service unavailable")
            self.attached_to.append(entity)
        return super().handler(request)


def _connect(org):
    QBOConnection.objects.create(organization=org, realm_id="123", access_token="tok", refresh_token="ref",
                                 access_expires_at=timezone.now() + timedelta(hours=1), default_expense_account_id="77")


def _client(org, fake):
    return QBOClient(QBOConnection.objects.get(organization=org), http=httpx.Client(transport=httpx.MockTransport(fake.handler)))


@pytest.mark.django_db
def test_credit_note_posts_as_vendor_credit(org, shipment):
    invoice = _freight(shipment)
    pdf, truth = extra.credit_note("CN-800", invoice.field("invoice_number"), vendor=_vendor(invoice))
    cn, _ = ingest_bytes(org, "credit.pdf", pdf, process="sync")
    shipment.status = Shipment.Status.APPROVED
    shipment.save()
    _connect(org)
    fake = FakeQBOWithCredits(fail_credit_once=True)
    first = post_shipment(shipment, client=_client(org, fake))
    assert first == {"posted": 2, "already_posted": 0, "failed": 1}
    failed = PostedBill.objects.get(document=cn)
    assert failed.kind == "vendor_credit" and failed.status == "failed" and "Amount must be positive" in failed.error
    second = post_shipment(Shipment.objects.get(pk=shipment.pk), client=_client(org, fake))
    assert second == {"posted": 1, "already_posted": 2, "failed": 0}
    assert len(fake.bills) == 2 and len(fake.credits) == 1
    credit = next(iter(fake.credits.values()))
    assert credit["DocNumber"] == "CN-800" and credit["VendorRef"]["value"]
    assert [line["Amount"] for line in credit["Line"]] == [120.0, 65.0]
    assert f"for invoice {invoice.field('invoice_number')}" in credit["PrivateNote"]
    assert fake.attached_to.count("VendorCredit") == 1 and fake.attached_to.count("Bill") == 2
    posted = PostedBill.objects.get(document=cn)
    assert posted.status == "posted" and posted.qbo_bill_id == "700"
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.POSTED
    # Posting again never creates a second credit (requestid), nor re-posts anything.
    assert post_shipment(shipment, client=_client(org, fake)) == {"posted": 0, "already_posted": 3, "failed": 0}
    assert len(fake.credits) == 1


@pytest.mark.django_db
def test_vendor_credit_retry_uses_the_same_request_id(org, shipment, monkeypatch):
    invoice = _freight(shipment)
    cn, _ = ingest_bytes(org, "credit.pdf", extra.credit_note("CN-801", invoice.field("invoice_number"),
                                                              vendor=_vendor(invoice))[0], process="sync")
    shipment.status = Shipment.Status.APPROVED
    shipment.save()
    _connect(org)
    attempts = quickbooks.MAX_ATTEMPTS
    monkeypatch.setattr(quickbooks, "MAX_ATTEMPTS", 1)  # the attachment upload fails for good on the first run
    fake = FakeQBOWithCredits(fail_credit_upload_once=True)
    assert post_shipment(shipment, client=_client(org, fake))["failed"] == 1
    monkeypatch.setattr(quickbooks, "MAX_ATTEMPTS", attempts)
    pb = PostedBill.objects.get(document=cn)
    assert pb.status == "failed" and pb.qbo_bill_id == "700"
    PostedBill.objects.filter(document=cn).update(qbo_bill_id="")  # as if the answer was lost after QuickBooks saved it
    assert post_shipment(Shipment.objects.get(pk=shipment.pk), client=_client(org, fake))["posted"] == 1
    credit_calls = [c for c in fake.calls if c[1] == "vendorcredit"]
    assert len(credit_calls) == 2 and len({c[2]["requestid"] for c in credit_calls}) == 1
    assert len(fake.credits) == 1 and PostedBill.objects.get(document=cn).qbo_bill_id == "700"


@pytest.mark.django_db
def test_shipment_page_shows_credit(client, user, org, shipment):
    invoice = _freight(shipment)
    ingest_bytes(org, "credit.pdf", extra.credit_note("CN-900", invoice.field("invoice_number"),
                                                      vendor=_vendor(invoice))[0], process="sync")
    client.force_login(user)
    page = client.get(reverse("review:shipment", args=[shipment.pk])).content.decode()
    assert "Credit notes" in page and "USD 185.00" in page and "taken off the payable total" in page
    assert "Credit note number" in page and "Original invoice number" in page
