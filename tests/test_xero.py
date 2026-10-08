"""Xero: OAuth (state, PKCE, tenants, rotating refresh, revoke), posting bills and credit notes against a fake
Xero API (httpx.MockTransport), rate limits, validation errors, the accounting settings page and the provider
switch. Payment status is in test_payments.py."""
import base64
import hashlib
import json
import re
import uuid
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, parse_qsl, unquote, urlparse

import httpx
import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.accounting.models import PostedBill, QBOConnection, VendorMapping, XeroConnection
from apps.accounting.services import posting, xero
from apps.accounting.services.posting import PostingBlocked, build_bill_payload, post_shipment, register_line_builder
from apps.accounting.services.providers import active_connection
from apps.accounting.services.xero import XeroClient
from apps.accounting.services.xero_posting import build_invoice_payload
from apps.core.models import AuditEvent
from apps.documents.models import ExtractedField
from apps.documents.services.ingest import ingest_bytes
from apps.notifications import delivery as delivery_mod
from apps.notifications.models import Delivery
from apps.shipments.models import Shipment
from synthetic import extra
from synthetic.generator import FORWARDERS
from tests.test_notifications import Hooks, _channel

TENANT = "11111111-2222-3333-4444-555555555555"
TENANT2 = "66666666-7777-8888-9999-000000000000"


def jwt(claims: dict) -> str:
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'RS256'})}.{part(claims)}.sig"


def validation(*messages):
    return {"ErrorNumber": 10, "Type": "ValidationException", "Message": "A validation exception occurred",
            "Elements": [{"ValidationErrors": [{"Message": m} for m in messages]}]}


class FakeXero:
    """Minimal Xero: identity (token, revocation), connections and the accounting API. Honours
    Idempotency-Key like the real API and records every request."""

    def __init__(self, tenants=None, currencies=("USD",), contacts=None):
        self.tenants = tenants if tenants is not None else [
            {"id": "conn-1", "tenantId": TENANT, "tenantType": "ORGANISATION", "tenantName": "Demo Company (US)"}]
        self.currencies = list(currencies)
        self.contacts = {c["Name"]: c for c in (contacts or [])}
        self.invoices, self.credit_notes, self.by_key = {}, {}, {}
        self.attachments, self.history, self.requests, self.revoked, self.token_forms = [], [], [], [], []
        self.access, self.refresh_token, self.n = "tok-0", "ref", 0
        self.challenge = ""                  # set by tests that check PKCE
        self.token_error = ""                # e.g. "invalid_grant"
        self.reject_next_api = 0             # answer 401 this many times
        self.throttles = []                  # [(problem, retry_after)] answered as 429s
        self.invoice_errors = []             # validation messages for the next PUT Invoices
        self.fail_upload_once = False
        self.basic_auth = None

    # ---- identity
    def _token(self, request):
        form = dict(parse_qsl(request.content.decode()))
        self.token_forms.append(form)
        self.basic_auth = request.headers.get("authorization")
        if self.token_error:
            return httpx.Response(400, json={"error": self.token_error})
        if form["grant_type"] == "authorization_code" and self.challenge:
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == self.challenge
        if form["grant_type"] == "refresh_token":
            assert form["refresh_token"] == self.refresh_token, "an old refresh token was spent"
        self.n += 1
        self.access = jwt({"authentication_event_id": f"event-{self.n}", "n": self.n})
        self.refresh_token = f"ref-{self.n}"
        return httpx.Response(200, json={"access_token": self.access, "refresh_token": self.refresh_token,
                                         "expires_in": 1800, "token_type": "Bearer"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "identity.xero.com" and path == "/connect/token":
            return self._token(request)
        if host == "identity.xero.com" and path == "/connect/revocation":
            self.revoked.append(dict(parse_qsl(request.content.decode()))["token"])
            return httpx.Response(200)
        if host == "developer.api.intuit.com":   # QuickBooks revoke when Xero replaces it
            return httpx.Response(200)
        if host == "api.xero.com" and path == "/connections":
            assert request.headers["authorization"] == f"Bearer {self.access}"
            return httpx.Response(200, json=self.tenants)
        assert host == "api.xero.com" and path.startswith("/api.xro/2.0/"), request.url
        api = unquote(path.split("/api.xro/2.0/", 1)[1])
        self.requests.append((request.method, api, dict(request.url.params), dict(request.headers)))
        if self.reject_next_api:
            self.reject_next_api -= 1
            return httpx.Response(401, json={"Title": "Unauthorized", "Status": 401, "Detail": "TokenExpired"})
        if request.headers.get("authorization") != f"Bearer {self.access}":
            return httpx.Response(401, json={"Title": "Unauthorized", "Status": 401})
        assert request.headers["xero-tenant-id"] in {t["tenantId"] for t in self.tenants}
        if self.throttles:
            problem, wait = self.throttles.pop(0)
            return httpx.Response(429, text="Rate limit exceeded",
                                  headers={"Retry-After": wait, "X-Rate-Limit-Problem": problem})
        headers = {"X-DayLimit-Remaining": "4990", "X-MinLimit-Remaining": "58"}
        return self.api(request, api, headers)

    def api(self, request, api, headers):
        key = request.headers.get("idempotency-key", "")
        if api == "Organisation":
            return httpx.Response(200, headers=headers, json={"Organisations": [
                {"Name": "Demo Company (US)", "BaseCurrency": "USD", "ShortCode": "!aBc12"}]})
        if api == "Currencies":
            return httpx.Response(200, headers=headers, json={"Currencies": [{"Code": c} for c in self.currencies]})
        if api == "Accounts":
            return httpx.Response(200, headers=headers, json={"Accounts": [
                {"Code": "310", "Name": "Freight and courier", "Type": "DIRECTCOSTS", "Class": "EXPENSE", "Status": "ACTIVE"},
                {"Code": "429", "Name": "General expenses", "Type": "EXPENSE", "Class": "EXPENSE", "Status": "ACTIVE"},
                {"Name": "No code", "Type": "OVERHEADS", "Class": "EXPENSE", "Status": "ACTIVE"}]})
        if api == "Contacts" and request.method == "GET":
            assert request.url.params.get("includeArchived") == "true"
            name = re.match(r'Name=="(.*)"$', request.url.params["where"]).group(1)
            found = [c for n, c in self.contacts.items() if n.lower() == name.lower()]
            return httpx.Response(200, headers=headers, json={"Contacts": found})
        if api == "Contacts" and request.method == "PUT":
            body = json.loads(request.content)["Contacts"][0]
            contact = {"ContactID": str(uuid.uuid4()), "Name": body["Name"], "ContactStatus": "ACTIVE",
                       "DefaultCurrency": body.get("DefaultCurrency", "")}
            self.contacts[body["Name"]] = contact
            return httpx.Response(200, headers=headers, json={"Contacts": [contact]})
        if api.startswith("Contacts/"):
            found = [c for c in self.contacts.values() if c["ContactID"] == api.split("/")[1]]
            return httpx.Response(200 if found else 404, headers=headers, json={"Contacts": found})
        if api in ("Invoices", "CreditNotes") and request.method == "PUT":
            return self._create(api, request, key, headers)
        if api == "Invoices" and request.method == "GET":
            params = request.url.params
            if "IDs" in params:
                wanted = set(params["IDs"].split(","))
                assert set(params["Statuses"].split(",")) >= {"VOIDED", "DELETED", "PAID"}
                return httpx.Response(200, headers=headers, json={"Invoices": [
                    i for i in self.invoices.values() if i["InvoiceID"] in wanted]})
            rows = [i for i in self.invoices.values() if i["Contact"]["ContactID"] == params["ContactIDs"]
                    and i.get("InvoiceNumber") == params["InvoiceNumbers"] and i["Status"] != "DELETED"]
            return httpx.Response(200, headers=headers, json={"Invoices": rows})
        if api == "CreditNotes" and request.method == "GET":
            ids = re.findall(r'CreditNoteID==Guid\("([0-9a-f-]+)"\)', request.url.params["where"])
            return httpx.Response(200, headers=headers, json={"CreditNotes": [
                c for c in self.credit_notes.values() if c["CreditNoteID"] in ids]})
        m = re.match(r"(Invoices|CreditNotes)/([0-9a-f-]+)/(Attachments|History)(?:/(.+))?$", api)
        if m and m.group(3) == "Attachments":
            if self.fail_upload_once:
                self.fail_upload_once = False
                return httpx.Response(400, json={"Message": "The file couldn't be read"})
            assert request.headers["content-type"] == "application/pdf" and request.content.startswith(b"%PDF")
            self.attachments.append((m.group(1), m.group(2), m.group(4)))
            return httpx.Response(200, headers=headers, json={"Attachments": [
                {"AttachmentID": str(uuid.uuid4()), "FileName": m.group(4)}]})
        if m and m.group(3) == "History":
            self.history.append((m.group(1), m.group(2), json.loads(request.content)["HistoryRecords"][0]["Details"]))
            return httpx.Response(200, headers=headers, json={"HistoryRecords": []})
        return httpx.Response(404, text="The resource you're looking for cannot be found")

    def _create(self, api, request, key, headers):
        body = json.loads(request.content)[api][0]
        if api == "Invoices" and self.invoice_errors:
            errors, self.invoice_errors = self.invoice_errors, []
            return httpx.Response(400, json=validation(*errors))
        store = self.invoices if api == "Invoices" else self.credit_notes
        if key and key in self.by_key:
            return httpx.Response(200, headers=headers, json={api: [store[self.by_key[key]]]})
        new_id = str(uuid.uuid4())
        total = round(sum(li["UnitAmount"] * li["Quantity"] for li in body["LineItems"]), 2)
        record = {**body, "Total": total, "Status": body["Status"]}
        if api == "Invoices":
            record.update({"InvoiceID": new_id, "AmountDue": total, "AmountPaid": 0, "AmountCredited": 0})
        else:
            record.update({"CreditNoteID": new_id, "RemainingCredit": total})
        store[new_id] = record
        if key:
            self.by_key[key] = new_id
        return httpx.Response(200, headers=headers, json={api: [record]})

    def calls(self, method, api):
        return [r for r in self.requests if r[0] == method and r[1] == api]


@pytest.fixture
def alert_hooks(monkeypatch):
    """Fake Slack/Teams for alerts."""
    h = Hooks()
    monkeypatch.setattr(delivery_mod, "_TRANSPORT", httpx.MockTransport(h))
    return h


@pytest.fixture
def xero_settings(settings):
    settings.XERO_CLIENT_ID, settings.XERO_CLIENT_SECRET = "xero-client", "xero-secret"
    settings.XERO_REDIRECT_URI = "http://testserver/accounting/xero/callback"
    settings.SITE_URL = "https://ap.example.com"
    return settings


@pytest.fixture
def approved(org, dataset):
    for f in ["S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.pdf", "S01_3_freight_invoice.pdf"]:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    s = Shipment.objects.get(organization=org)
    s.status = Shipment.Status.APPROVED
    s.save()
    return s


def connect(org, **kw):
    fields = {"tenant_id": TENANT, "tenant_name": "Demo Company (US)", "connection_id": "conn-1",
              "tenants": [{"id": "conn-1", "tenantId": TENANT, "tenantName": "Demo Company (US)"}],
              "access_token": "tok-0", "refresh_token": "ref", "access_expires_at": timezone.now() + timedelta(minutes=25),
              "default_account_code": "310", "home_currency": "USD", "currencies": ["USD"], "short_code": "!aBc12"}
    return XeroConnection.objects.create(organization=org, **{**fields, **kw})


def client_for(org, fake, sleeps=None):
    conn = XeroConnection.objects.get(organization=org)
    return XeroClient(conn, http=httpx.Client(transport=httpx.MockTransport(fake.handler)),
                      sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


@pytest.fixture
def patched_http(monkeypatch):
    """Every httpx.Client the code opens on its own talks to this fake (views and commands)."""
    fake = FakeXero()
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real(transport=httpx.MockTransport(fake.handler)))
    monkeypatch.setattr(xero.time, "sleep", lambda s: None)
    return fake


# --------------------------------------------------------------------------- OAuth


@pytest.mark.django_db
def test_connect_with_pkce_and_single_organisation_replaces_quickbooks(client, org, admin_user, xero_settings,
                                                                       patched_http):
    xero_settings.XERO_CLIENT_SECRET = ""   # a PKCE app
    QBOConnection.objects.create(organization=org, realm_id="123", access_token="a", refresh_token="r",
                                 access_expires_at=timezone.now() + timedelta(hours=1))
    client.force_login(admin_user)
    r = client.get(reverse("accounting:xero_connect", args=[org.pk]))
    assert r.status_code == 302 and r.url.startswith(xero.AUTH_URL)
    q = {k: v[0] for k, v in parse_qs(urlparse(r.url).query).items()}
    assert q["scope"] == xero_settings.XERO_SCOPES and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == xero_settings.XERO_REDIRECT_URI and q["client_id"] == "xero-client"
    patched_http.challenge = q["code_challenge"]
    r = client.get(reverse("accounting:xero_callback"), {"code": "the-code", "state": q["state"]})
    assert r.status_code == 302 and r.url == reverse("accounting:settings", args=[org.pk])
    form = patched_http.token_forms[0]
    assert form["client_id"] == "xero-client" and form["code_verifier"] and patched_http.basic_auth is None
    conn = XeroConnection.objects.get(organization=org)
    assert conn.tenant_id == TENANT and conn.tenant_name == "Demo Company (US)" and conn.home_currency == "USD"
    assert conn.refresh_token == "ref-1" and conn.access_token == patched_http.access   # stored (encrypted)
    assert not QBOConnection.objects.filter(organization=org).exists()   # one active posting target
    assert active_connection(org).system == "xero"
    actions = list(AuditEvent.objects.filter(organization=org).values_list("action", flat=True))
    assert "xero.connected" in actions and "qbo.disconnected" in actions


@pytest.mark.django_db
def test_tokens_are_encrypted_at_rest(org):
    connect(org, refresh_token="very-secret-refresh")
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("select refresh_token, access_token from accounting_xeroconnection")
        stored = cur.fetchone()
    assert "very-secret-refresh" not in stored[0] and "tok-0" not in stored[1]
    assert XeroConnection.objects.get(organization=org).refresh_token == "very-secret-refresh"


@pytest.mark.django_db
def test_client_secret_app_uses_basic_auth_and_no_pkce(client, org, admin_user, xero_settings, patched_http):
    client.force_login(admin_user)
    r = client.get(reverse("accounting:xero_connect", args=[org.pk]))
    q = {k: v[0] for k, v in parse_qs(urlparse(r.url).query).items()}
    assert "code_challenge" not in q
    client.get(reverse("accounting:xero_callback"), {"code": "c", "state": q["state"]})
    expected = "Basic " + base64.b64encode(b"xero-client:xero-secret").decode()
    assert patched_http.basic_auth == expected and "code_verifier" not in patched_http.token_forms[0]
    assert XeroConnection.objects.get(organization=org).tenant_id == TENANT


@pytest.mark.django_db
def test_callback_checks_state(client, org, admin_user, xero_settings, patched_http):
    client.force_login(admin_user)
    client.get(reverse("accounting:xero_connect", args=[org.pk]))
    r = client.get(reverse("accounting:xero_callback"), {"code": "c", "state": "forged"}, follow=True)
    assert "expired or was opened twice" in r.content.decode()
    assert patched_http.token_forms == [] and not XeroConnection.objects.exists()


@pytest.mark.django_db
def test_several_organisations_let_the_admin_choose(client, org, admin_user, xero_settings, patched_http):
    patched_http.tenants.append({"id": "conn-2", "tenantId": TENANT2, "tenantType": "ORGANISATION",
                                 "tenantName": "Second Ltd"})
    patched_http.tenants.append({"id": "conn-3", "tenantId": "x", "tenantType": "PRACTICEMANAGER",
                                 "tenantName": "Practice Manager Org"})
    client.force_login(admin_user)
    r = client.get(reverse("accounting:xero_connect", args=[org.pk]))
    state = parse_qs(urlparse(r.url).query)["state"][0]
    r = client.get(reverse("accounting:xero_callback"), {"code": "c", "state": state})
    assert r.url == reverse("accounting:xero_tenant", args=[org.pk])
    conn = XeroConnection.objects.get(organization=org)
    assert conn.tenant_id == "" and [t["tenantName"] for t in conn.tenants] == ["Demo Company (US)", "Second Ltd"]
    assert active_connection(org) is None   # nothing posts until one is chosen
    page = client.get(r.url).content.decode()
    assert "Second Ltd" in page and "Practice Manager Org" not in page
    r = client.post(r.url, {"tenant_id": "not-offered"})
    assert XeroConnection.objects.get(organization=org).tenant_id == ""
    client.post(reverse("accounting:xero_tenant", args=[org.pk]), {"tenant_id": TENANT2})
    conn.refresh_from_db()
    assert conn.tenant_id == TENANT2 and conn.connection_id == "conn-2"


@pytest.mark.django_db
def test_cancelled_and_invalid_scope_are_explained(client, org, admin_user, xero_settings, patched_http):
    client.force_login(admin_user)
    for error, text in (("access_denied", "Xero connection was cancelled"), ("invalid_scope", "XERO_SCOPES")):
        r = client.get(reverse("accounting:xero_connect", args=[org.pk]))
        state = parse_qs(urlparse(r.url).query)["state"][0]
        r = client.get(reverse("accounting:xero_callback"), {"error": error, "state": state}, follow=True)
        assert text in r.content.decode()
    assert not XeroConnection.objects.exists()


@pytest.mark.django_db
def test_only_admins_connect_or_disconnect(client, org, approver, xero_settings, patched_http):
    connect(org)
    client.force_login(approver)
    assert client.get(reverse("accounting:xero_connect", args=[org.pk])).status_code == 403
    assert client.post(reverse("accounting:xero_disconnect", args=[org.pk])).status_code == 403
    assert client.get(reverse("accounting:settings", args=[org.pk])).status_code == 403
    assert XeroConnection.objects.filter(organization=org).exists()


@pytest.mark.django_db
def test_disconnect_revokes_the_refresh_token(client, org, admin_user, xero_settings, patched_http):
    connect(org, refresh_token="ref-to-revoke")
    client.force_login(admin_user)
    r = client.post(reverse("accounting:xero_disconnect", args=[org.pk]), follow=True)
    assert "Xero disconnected" in r.content.decode()
    assert patched_http.revoked == ["ref-to-revoke"] and not XeroConnection.objects.exists()
    event = AuditEvent.objects.get(action="xero.disconnected")
    assert event.data["revoked_at_xero"] is True and event.actor == admin_user


@pytest.mark.django_db
def test_refresh_rotates_and_never_spends_a_token_twice(org, xero_settings):
    conn = connect(org, access_expires_at=timezone.now() - timedelta(minutes=1))
    fake = FakeXero()
    client = client_for(org, fake)
    client.organisation()                       # expired: refreshed first
    conn.refresh_from_db()
    assert conn.refresh_token == "ref-1" and conn.access_valid
    assert conn.access_expires_at < timezone.now() + timedelta(minutes=31)   # 30-minute access tokens
    stale = XeroConnection.objects.get(pk=conn.pk)
    stale.access_token = "old"                  # another worker already refreshed: the lock sees the new token
    stale.access_expires_at = timezone.now() - timedelta(minutes=1)
    xero.refresh(stale, httpx.Client(transport=httpx.MockTransport(fake.handler)))
    assert len(fake.token_forms) == 1 and stale.refresh_token == "ref-1"


@pytest.mark.django_db
def test_invalid_grant_marks_needs_reconnect_audits_once_and_alerts(org, approved, xero_settings, alert_hooks,
                                                                    django_capture_on_commit_callbacks):
    _channel(org, events_=["xero.needs_reconnect"])
    connect(org, access_expires_at=timezone.now() - timedelta(minutes=1))
    fake = FakeXero()
    fake.token_error = "invalid_grant"
    with django_capture_on_commit_callbacks(execute=True):
        summary = post_shipment(approved, client=client_for(org, fake))
        post_shipment(approved, client=client_for(org, fake))
    assert summary["failed"] == 1   # stops after the first: every bill would fail the same way
    conn = XeroConnection.objects.get(organization=org)
    assert conn.needs_reconnect and "connected again" in conn.last_error
    assert "connected again" in PostedBill.objects.get(status="failed").error
    assert AuditEvent.objects.filter(action="xero.needs_reconnect").count() == 1
    assert Delivery.objects.filter(event="xero.needs_reconnect").count() == 1
    assert alert_hooks.json()["blocks"][0]["text"]["text"] == "Xero needs to be connected again"


# --------------------------------------------------------------------------- posting


@pytest.mark.django_db
def test_posts_bills_with_contacts_lines_attachments_and_notes(org, approved, xero_settings):
    connect(org)
    fake = FakeXero()
    summary = post_shipment(approved, client=client_for(org, fake))
    assert summary == {"posted": 2, "already_posted": 0, "failed": 0}   # the B/L is not a bill
    assert len(fake.invoices) == 2 and len(fake.attachments) == 2 and len(fake.history) == 2
    assert len(fake.contacts) == 2 and VendorMapping.objects.exclude(xero_contact_id="").count() == 2
    for pb in PostedBill.objects.filter(organization=org):
        invoice = fake.invoices[pb.qbo_bill_id]
        doc = pb.document
        assert pb.system == "xero" and pb.ledger_id == TENANT and pb.status == "posted"
        assert invoice["Type"] == "ACCPAY" and invoice["Status"] == "DRAFT" and invoice["LineAmountTypes"] == "NoTax"
        assert invoice["InvoiceNumber"] == doc.field("invoice_number") == pb.external_number
        assert invoice["CurrencyCode"] == "USD" and invoice["Date"] == doc.field("invoice_date")
        assert {li["AccountCode"] for li in invoice["LineItems"]} == {"310"}
        assert Decimal(str(invoice["Total"])) == Decimal(str(doc.field("total_amount")))
        assert invoice["Url"] == f"https://ap.example.com{reverse('review:shipment', args=[approved.pk])}"
        put = next(r for r in fake.calls("PUT", "Invoices") if r[3]["idempotency-key"] == pb.request_id)
        assert put[3]["xero-tenant-id"] == TENANT
    assert all(name.endswith(".pdf") for _, _, name in fake.attachments)
    assert approved.reference in fake.history[0][2]
    approved.refresh_from_db()
    assert approved.status == Shipment.Status.POSTED
    event = AuditEvent.objects.filter(action="bill.posted").first()
    assert event.data["system"] == "Xero"
    from apps.shipments.labels import describe_action

    assert describe_action("bill.posted", event.data).endswith("to Xero")
    assert describe_action("bill.posted", {"qbo_bill_id": "500"}) == "posted bill 500 to QuickBooks"


@pytest.mark.django_db
def test_authorised_bills_and_existing_contact(org, approved, xero_settings):
    connect(org, bill_status="AUTHORISED")
    doc = next(d for d in approved.documents if d.doc_type == "freight_invoice")
    existing = {"ContactID": str(uuid.uuid4()), "Name": doc.field("vendor_name").upper(), "ContactStatus": "ACTIVE"}
    fake = FakeXero(contacts=[existing])
    post_shipment(approved, client=client_for(org, fake))
    assert len(fake.contacts) == 2   # found by name (any case), only the other vendor was created
    pb = PostedBill.objects.get(document=doc)
    assert fake.invoices[pb.qbo_bill_id]["Contact"]["ContactID"] == existing["ContactID"]
    assert all(i["Status"] == "AUTHORISED" for i in fake.invoices.values())


@pytest.mark.django_db
def test_archived_contact_blocks_with_a_clear_message(org, approved, xero_settings):
    connect(org)
    doc = next(d for d in approved.documents if d.doc_type == "freight_invoice")
    fake = FakeXero(contacts=[{"ContactID": str(uuid.uuid4()), "Name": doc.field("vendor_name"),
                               "ContactStatus": "ARCHIVED"}])
    summary = post_shipment(approved, client=client_for(org, fake))
    assert summary["failed"] == 1 and summary["posted"] == 1
    assert "is archived in Xero. Restore it" in PostedBill.objects.get(document=doc).error


@pytest.mark.django_db
def test_retry_never_creates_a_second_bill(org, approved, xero_settings):
    connect(org)
    fake = FakeXero()
    fake.fail_upload_once = True
    first = post_shipment(approved, client=client_for(org, fake))
    assert first["failed"] == 1 and len(fake.invoices) == 2
    failed = PostedBill.objects.get(status="failed")
    assert failed.qbo_bill_id and "couldn't be read" in failed.error
    second = post_shipment(Shipment.objects.get(pk=approved.pk), client=client_for(org, fake))
    assert second == {"posted": 1, "already_posted": 1, "failed": 0} and len(fake.invoices) == 2
    # The answer to an earlier attempt was lost and the Idempotency-Key expired: the bill is found, not re-created.
    pb = PostedBill.objects.get(pk=failed.pk)
    original = pb.qbo_bill_id
    PostedBill.objects.filter(pk=pb.pk).update(status="failed", qbo_bill_id="", qbo_attachable_id="",
                                              error="Could not reach Xero", request_id=pb.request_id + "-x")
    fake.by_key.clear()
    approved.status = Shipment.Status.APPROVED
    approved.save()
    assert post_shipment(approved, client=client_for(org, fake))["posted"] == 1
    assert len(fake.invoices) == 2 and PostedBill.objects.get(pk=pb.pk).qbo_bill_id == original


@pytest.mark.django_db
def test_credit_note_posts_as_accpaycredit(org, approved, xero_settings):
    invoice = next(d for d in approved.documents if d.doc_type == "freight_invoice")
    vendor = next(p for p in FORWARDERS if p.name == invoice.field("vendor_name"))
    approved.status = Shipment.Status.OPEN   # approved shipments are locked; the credit note arrives first
    approved.save()
    cn, _ = ingest_bytes(org, "credit.pdf", extra.credit_note("CN-800", invoice.field("invoice_number"),
                                                              vendor=vendor)[0], process="sync")
    approved.status = Shipment.Status.APPROVED
    approved.save()
    connect(org)
    fake = FakeXero()
    assert post_shipment(approved, client=client_for(org, fake)) == {"posted": 3, "already_posted": 0, "failed": 0}
    pb = PostedBill.objects.get(document=cn)
    credit = fake.credit_notes[pb.qbo_bill_id]
    assert pb.kind == "vendor_credit" and credit["Type"] == "ACCPAYCREDIT" and credit["CreditNoteNumber"] == "CN-800"
    assert [li["UnitAmount"] for li in credit["LineItems"]] == [120.0, 65.0]
    assert ("CreditNotes", pb.qbo_bill_id) in [(a[0], a[1]) for a in fake.attachments]
    assert f"credits invoice {invoice.field('invoice_number')}" in next(h[2] for h in fake.history if h[0] == "CreditNotes")


@pytest.mark.django_db
def test_foreign_currency_needs_the_currency_in_xero(org, approved, xero_settings):
    connect(org)
    doc = next(d for d in approved.documents if d.doc_type == "commercial_invoice")
    ExtractedField.objects.update_or_create(document=doc, name="currency", defaults={"value": "EUR", "confidence": 1})
    summary = post_shipment(approved, client=client_for(org, FakeXero()))
    assert summary["failed"] == 1 and summary["posted"] == 1
    assert "EUR isn't added in Xero" in PostedBill.objects.get(document=doc).error
    # With EUR added in Xero it posts in EUR, and a new contact gets EUR as its default currency.
    XeroConnection.objects.filter(organization=org).update(currencies=["USD", "EUR"])
    fake = FakeXero(currencies=("USD", "EUR"))
    assert post_shipment(Shipment.objects.get(pk=approved.pk), client=client_for(org, fake))["posted"] == 1
    invoice = next(iter(fake.invoices.values()))
    assert invoice["CurrencyCode"] == "EUR" and next(iter(fake.contacts.values()))["DefaultCurrency"] == "EUR"


@pytest.mark.django_db
def test_validation_errors_are_shown_in_plain_words(org, approved, xero_settings):
    connect(org)
    fake = FakeXero()
    fake.invoice_errors = ["Account code '310' is not a valid code for this document."]
    summary = post_shipment(approved, client=client_for(org, fake))
    assert summary["failed"] == 1
    error = PostedBill.objects.get(status="failed").error
    assert error.startswith("Xero won't accept that expense account on a bill")
    assert "Xero said: Account code '310' is not a valid code for this document." in error


@pytest.mark.django_db
def test_minute_limit_waits_and_retries(org, approved, xero_settings):
    connect(org)
    fake = FakeXero()
    fake.throttles = [("minute", "3")]
    sleeps = []
    assert post_shipment(approved, client=client_for(org, fake, sleeps))["posted"] == 2
    assert sleeps == [3.0]


@pytest.mark.django_db
def test_daily_limit_stops_the_run(org, approved, xero_settings):
    connect(org)
    fake = FakeXero()
    fake.throttles = [("day", "40000")]
    sleeps = []
    summary = post_shipment(approved, client=client_for(org, fake, sleeps))
    assert summary == {"posted": 0, "already_posted": 0, "failed": 1} and sleeps == []
    assert "daily limit" in PostedBill.objects.get(status="failed").error


@pytest.mark.django_db
def test_401_refreshes_once_and_retries(org, approved, xero_settings):
    conn = connect(org)
    fake = FakeXero()
    fake.reject_next_api = 1   # the token looks valid but Xero refuses it
    assert post_shipment(approved, client=client_for(org, fake))["posted"] == 2
    conn.refresh_from_db()
    assert len(fake.token_forms) == 1 and conn.refresh_token == "ref-1" and not conn.needs_reconnect


@pytest.mark.django_db
def test_no_expense_account_and_no_vendor_name(org, approved, xero_settings):
    connect(org, default_account_code="")
    summary = post_shipment(approved, client=client_for(org, FakeXero()))
    assert summary["failed"] == 2
    assert "No expense account" in PostedBill.objects.first().error


@pytest.mark.django_db
def test_switching_systems_never_posts_a_bill_twice(org, approved, xero_settings):
    doc = next(d for d in approved.documents if d.doc_type == "freight_invoice")
    PostedBill.objects.create(organization=org, document=doc, shipment=approved, request_id="sm-x", system="quickbooks",
                              status="failed", qbo_bill_id="500", error="attachment failed")
    connect(org)
    fake = FakeXero()
    summary = post_shipment(approved, client=client_for(org, fake))
    assert summary == {"posted": 1, "already_posted": 0, "failed": 1}
    assert "already created in QuickBooks (500)" in PostedBill.objects.get(document=doc).error
    assert len(fake.invoices) == 1


@pytest.mark.django_db
def test_public_demo_never_calls_xero(client, org, admin_user, xero_settings, patched_http, settings):
    settings.DEMO_MODE, settings.DEMO_SEND_OUTSIDE = True, False
    client.force_login(admin_user)
    r = client.get(reverse("accounting:xero_connect", args=[org.pk]), follow=True)
    assert "public demo" in r.content.decode() and patched_http.token_forms == []
    connect(org)
    with pytest.raises(xero.XeroError, match="public demo"):
        XeroClient(XeroConnection.objects.get(organization=org)).organisation()
    assert patched_http.requests == []


# --------------------------------------------------------------------------- bill line builders


@pytest.mark.django_db
def test_registered_line_builder_shapes_both_systems(org, approved, monkeypatch):
    monkeypatch.setattr(posting, "LINE_BUILDERS", [])
    doc = next(d for d in approved.documents if d.doc_type == "freight_invoice")
    total = Decimal(str(doc.field("total_amount")))

    def allocate(d):
        if d.pk != doc.pk:
            return None   # other documents keep the default lines
        return [{"description": "Ocean freight", "amount": total - Decimal("100.00"), "memo": approved.reference,
                 "class": "7", "tracking": [("Shipment", approved.reference)]},
                ("Ocean freight", "90.00", "SHP-000999")]

    register_line_builder(allocate)
    register_line_builder(allocate)   # registering twice is harmless
    assert posting.LINE_BUILDERS == [allocate]
    qbo = build_bill_payload(doc, approved, "100", "77")["Line"]
    assert [line["Description"] for line in qbo] == [f"Ocean freight ({approved.reference})",
                                                     "Ocean freight (SHP-000999)", "Adjustment to the printed invoice total"]
    assert qbo[0]["AccountBasedExpenseLineDetail"]["ClassRef"] == {"value": "7"} and qbo[2]["Amount"] == 10.0
    conn = connect(org)
    lines = build_invoice_payload(doc, approved, "contact", "310", conn)["LineItems"]
    assert lines[0]["Tracking"] == [{"Name": "Shipment", "Option": approved.reference}]
    assert round(sum(li["UnitAmount"] for li in lines), 2) == float(total)
    other = next(d for d in approved.documents if d.doc_type == "commercial_invoice")
    assert "ClassRef" not in build_bill_payload(other, approved, "100", "77")["Line"][0]["AccountBasedExpenseLineDetail"]

    def broken(d):
        raise ValueError("bad split")

    monkeypatch.setattr(posting, "LINE_BUILDERS", [broken])
    with pytest.raises(PostingBlocked, match="couldn't be prepared"):
        build_bill_payload(doc, approved, "100", "77")


# --------------------------------------------------------------------------- pages


@pytest.mark.django_db
def test_settings_page_shows_both_systems_and_saves_xero_settings(client, org, admin_user, xero_settings,
                                                                  patched_http):
    xero_settings.QBO_CLIENT_ID, xero_settings.QBO_CLIENT_SECRET = "qbo-id", "qbo-secret"
    connect(org, default_account_code="")
    client.force_login(admin_user)
    page = client.get(reverse("accounting:settings", args=[org.pk])).content.decode()
    assert "QuickBooks Online" in page and "Connect QuickBooks instead" in page
    assert "Demo Company (US)" in page and "310 Freight and courier (Direct costs)" in page and "No code" not in page
    assert xero_settings.XERO_REDIRECT_URI in page and ">Accounting</a>" in page
    r = client.post(reverse("accounting:xero_settings", args=[org.pk]),
                    {"default_account_code": "429", "bill_status": "AUTHORISED"}, follow=True)
    assert "Xero settings saved" in r.content.decode()
    conn = XeroConnection.objects.get(organization=org)
    assert conn.default_account_code == "429" and conn.bill_status == "AUTHORISED"
    assert AuditEvent.objects.get(action="xero.settings_updated").data["account"] == "429"
    client.post(reverse("accounting:xero_settings", args=[org.pk]), {"default_account_code": "1", "bill_status": "PAID"})
    assert XeroConnection.objects.get(organization=org).bill_status == "AUTHORISED"


@pytest.mark.django_db
def test_shipment_page_says_post_to_xero_and_rules_use_xero_codes(client, org, approved, approver, xero_settings):
    connect(org, default_account_code="429")
    client.force_login(approver)
    page = client.get(reverse("review:shipment", args=[approved.pk])).content.decode()
    assert "Post bills to Xero" in page and "Post bills to QuickBooks" not in page and "Xero code" in page
    doc = next(d for d in approved.documents if d.doc_type == "freight_invoice")
    client.post(reverse("review:set_account", args=[doc.pk]), {"account_id": "310", "account_name": "Freight"})
    mapping = VendorMapping.objects.get(organization=org)
    assert mapping.xero_account_code == "310" and mapping.expense_account_id == ""
    fake = FakeXero()
    post_shipment(approved, client=client_for(org, fake))
    pb = PostedBill.objects.get(document=doc)
    assert {li["AccountCode"] for li in fake.invoices[pb.qbo_bill_id]["LineItems"]} == {"310"}
    page = client.get(reverse("review:shipment", args=[approved.pk])).content.decode()
    assert f"Xero: Posted, bill {doc.field('invoice_number')}" in page
    assert "go.xero.com/organisationlogin/default.aspx?shortcode=!aBc12" in page


@pytest.mark.django_db
def test_xero_check_command(org, approved, xero_settings, patched_http, capsys):
    connect(org)
    call_command("xero_check", org="test", post=approved.reference)
    out = capsys.readouterr().out
    assert "Connected to Demo Company (US)" in out and "2 expense accounts" in out
    assert "calls left today" in out and "'posted': 2" in out
