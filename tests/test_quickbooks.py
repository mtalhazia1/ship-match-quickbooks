"""QuickBooks posting against a fake QuickBooks API (httpx.MockTransport)."""
import json
import re
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from django.utils import timezone

from apps.accounting.models import PostedBill, QBOConnection, VendorMapping
from apps.accounting.services import quickbooks
from apps.accounting.services.posting import PostingBlocked, build_bill_payload, post_shipment
from apps.accounting.services.quickbooks import QBOClient
from apps.documents.models import ExtractedField
from apps.documents.services.ingest import ingest_bytes
from apps.shipments.models import Shipment


def fault(code, message, detail=""):
    return {"Fault": {"Error": [{"Message": message, "Detail": detail, "code": code}], "type": "ValidationFault"}}


class FakeQBO:
    """Minimal QuickBooks: honors requestid idempotency like the real API."""

    def __init__(self, fail_upload_once=False, throttle_once=False, multicurrency=False, home="USD",
                 vendor_name_taken=False):
        self.bills, self.by_request, self.uploads, self.vendors, self.calls = {}, {}, [], {}, []
        self.fail_upload_once, self.throttle_once = fail_upload_once, throttle_once
        self.multicurrency, self.home, self.vendor_name_taken = multicurrency, home, vendor_name_taken

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/v3/company/123/")[-1]
        self.calls.append((request.method, path, dict(request.url.params)))
        assert request.url.params["minorversion"] == "75"
        if self.throttle_once:
            self.throttle_once = False
            return httpx.Response(429, text="Too many requests", headers={"retry-after": "0"})
        if path == "companyinfo/123":
            return httpx.Response(200, json={"CompanyInfo": {"CompanyName": "Sandbox Company_US_1"}})
        if path == "preferences":
            return httpx.Response(200, json={"Preferences": {"CurrencyPrefs": {
                "MultiCurrencyEnabled": self.multicurrency, "HomeCurrency": {"value": self.home}}}})
        if path == "query":
            name = re.search(r"DisplayName = '(.*?)'(?: and|$)", request.url.params["query"]).group(1)
            v = self.vendors.get(name)
            return httpx.Response(200, json={"QueryResponse": {"Vendor": [v]} if v else {}})
        if path == "vendor":
            if self.vendor_name_taken:
                return httpx.Response(400, json=fault("6240", "Duplicate Name Exists Error"))
            body = json.loads(request.content)
            v = {"Id": str(100 + len(self.vendors)), "DisplayName": body["DisplayName"], "Active": True,
                 "CurrencyRef": body.get("CurrencyRef") or {"value": self.home}}
            self.vendors[body["DisplayName"]] = v
            return httpx.Response(200, json={"Vendor": v})
        if path == "bill":
            rid = request.url.params["requestid"]
            if rid not in self.by_request:
                bill_id = str(500 + len(self.bills))
                self.bills[bill_id] = json.loads(request.content)
                self.by_request[rid] = bill_id
            return httpx.Response(200, json={"Bill": {"Id": self.by_request[rid]}})
        if path == "upload":
            if self.fail_upload_once:
                self.fail_upload_once = False
                return httpx.Response(400, json=fault("2500", "Invalid Reference Id"))
            self.uploads.append(request.headers["content-type"])
            return httpx.Response(200, json={"AttachableResponse": [{"Attachable": {"Id": str(900 + len(self.uploads))}}]})
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(quickbooks.time, "sleep", lambda s: None)


@pytest.fixture
def approved(org, dataset):
    for f in ["S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.pdf", "S01_3_freight_invoice.pdf"]:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    s = Shipment.objects.get(organization=org)
    s.status = Shipment.Status.APPROVED
    s.save()
    QBOConnection.objects.create(organization=org, realm_id="123", access_token="tok", refresh_token="ref",
                                 access_expires_at=timezone.now() + timedelta(hours=1),
                                 default_expense_account_id="77")
    return s


def _client(org, fake):
    return QBOClient(QBOConnection.objects.get(organization=org), http=httpx.Client(transport=httpx.MockTransport(fake.handler)))


@pytest.mark.django_db
def test_posts_one_bill_per_invoice_with_attachment(org, approved):
    fake = FakeQBO()
    summary = post_shipment(approved, client=_client(org, fake))
    assert summary == {"posted": 2, "already_posted": 0, "failed": 0}  # commercial + freight invoice; B/L is not a bill
    assert len(fake.bills) == 2 and len(fake.uploads) == 2
    bill = next(iter(fake.bills.values()))
    assert bill["Line"][0]["AccountBasedExpenseLineDetail"]["AccountRef"]["value"] == "77"
    assert approved.reference in bill["PrivateNote"]
    approved.refresh_from_db()
    assert approved.status == Shipment.Status.POSTED
    assert VendorMapping.objects.filter(organization=org).exclude(qbo_vendor_id="").count() == 2
    conn = QBOConnection.objects.get(organization=org)
    assert conn.company_name == "Sandbox Company_US_1" and conn.home_currency == "USD"


@pytest.mark.django_db
def test_retry_never_creates_a_second_bill(org, approved):
    fake = FakeQBO(fail_upload_once=True)
    first = post_shipment(approved, client=_client(org, fake))
    assert first["failed"] == 1
    failed = PostedBill.objects.get(status="failed")
    assert "Invalid Reference Id" in failed.error
    approved.refresh_from_db()
    second = post_shipment(approved, client=_client(org, fake))
    assert second["posted"] == 1 and second["already_posted"] == 1
    assert len(fake.bills) == 2  # still two bills
    third = post_shipment(Shipment.objects.get(pk=approved.pk), client=_client(org, fake))
    assert third == {"posted": 0, "already_posted": 2, "failed": 0}


@pytest.mark.django_db
def test_throttling_is_retried(org, approved):
    fake = FakeQBO(throttle_once=True)
    assert post_shipment(approved, client=_client(org, fake))["posted"] == 2


@pytest.mark.django_db
def test_unapproved_shipment_cannot_post(org, approved):
    approved.status = Shipment.Status.READY
    approved.save()
    with pytest.raises(PostingBlocked):
        post_shipment(approved, client=_client(org, FakeQBO()))


@pytest.mark.django_db
def test_bill_matches_printed_total_when_lines_do_not(org, approved):
    doc = next(d for d in approved.documents if d.doc_type == "freight_invoice")
    f = doc.fields.get(name="total_amount")
    printed = Decimal(str(f.value)) + Decimal("10.00")
    f.value = str(printed)
    f.save()
    payload = build_bill_payload(doc, approved, "100", "77")
    assert Decimal(str(round(sum(line["Amount"] for line in payload["Line"]), 2))) == printed
    assert payload["Line"][-1]["Description"] == "Adjustment to the printed invoice total"


@pytest.mark.django_db
def test_foreign_currency_needs_multicurrency(org, approved):
    doc = next(d for d in approved.documents if d.doc_type == "commercial_invoice")
    ExtractedField.objects.update_or_create(document=doc, name="currency", defaults={"value": "EUR", "confidence": 1})
    summary = post_shipment(approved, client=_client(org, FakeQBO(multicurrency=False)))
    assert summary["failed"] == 1 and summary["posted"] == 1
    assert "multicurrency is off" in PostedBill.objects.get(document=doc).error


@pytest.mark.django_db
def test_name_taken_gives_clear_message(org, approved):
    summary = post_shipment(approved, client=_client(org, FakeQBO(vendor_name_taken=True)))
    assert summary["failed"] == 2
    assert "already uses the name" in PostedBill.objects.filter(status="failed").first().error


@pytest.mark.django_db
def test_expired_connection_asks_for_reconnect(org, approved, settings):
    settings.QBO_CLIENT_ID, settings.QBO_CLIENT_SECRET = "id", "secret"
    conn = QBOConnection.objects.get(organization=org)
    conn.access_expires_at = timezone.now() - timedelta(minutes=5)
    conn.save()

    def token_endpoint(request):
        return httpx.Response(400, json={"error": "invalid_grant"})
    http = httpx.Client(transport=httpx.MockTransport(token_endpoint))
    summary = post_shipment(approved, client=QBOClient(conn, http=http))
    assert summary["failed"] == 1  # stops after the first: every bill would fail the same way
    conn.refresh_from_db()
    assert conn.needs_reconnect and "connected again" in PostedBill.objects.get(status="failed").error


@pytest.mark.django_db
def test_refresh_stores_rotated_token(org, approved, settings):
    settings.QBO_CLIENT_ID, settings.QBO_CLIENT_SECRET = "id", "secret"
    conn = QBOConnection.objects.get(organization=org)
    conn.access_expires_at = timezone.now() - timedelta(minutes=5)
    conn.save()

    def token_endpoint(request):
        assert b"refresh_token=ref" in request.content
        return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh",
                                         "expires_in": 3600, "x_refresh_token_expires_in": 8726400})
    quickbooks.refresh(conn, httpx.Client(transport=httpx.MockTransport(token_endpoint)))
    conn.refresh_from_db()
    assert conn.refresh_token == "new-refresh" and conn.access_valid and not conn.needs_reconnect
