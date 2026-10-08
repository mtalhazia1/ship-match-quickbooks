"""Payment status of posted bills, read from QuickBooks and Xero (fakes on httpx.MockTransport): batching,
statuses, voided and deleted bills with their alert, hourly limits, the check button, the shipment page, the
shipment list's Payments column and filter, and the dashboard's aging summary."""
import re
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal

import httpx
import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounting import tasks
from apps.accounting.models import PaymentSync, PostedBill, QBOConnection, XeroConnection
from apps.accounting.services import payments
from apps.accounting.services.providers import QuickBooksProvider, XeroProvider
from apps.accounting.services.quickbooks import QBOClient
from apps.core.models import AuditEvent
from apps.documents.models import Document, ExtractedField
from apps.notifications import delivery as delivery_mod
from apps.notifications.models import Delivery
from apps.shipments.models import MatchLink, Shipment
from tests.test_notifications import Hooks, _channel
from tests.test_quickbooks import fault
from tests.test_xero import TENANT, FakeXero, client_for
from tests.test_xero import connect as connect_xero

P = PostedBill.Payment


@pytest.fixture
def alert_hooks(monkeypatch):
    h = Hooks()
    monkeypatch.setattr(delivery_mod, "_TRANSPORT", httpx.MockTransport(h))
    return h


class FakeQBOPayments:
    """QuickBooks query endpoint for Bill, BillPayment and VendorCredit by Id, and single reads."""

    def __init__(self, bills=(), bill_payments=(), credits=(), deleted=()):
        self.store = {"Bill": {b["Id"]: b for b in bills}, "BillPayment": {p["Id"]: p for p in bill_payments},
                      "VendorCredit": {c["Id"]: c for c in credits}}
        self.deleted, self.queries, self.reads = set(deleted), [], []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/v3/company/123/")[-1]
        if path == "query":
            q = request.url.params["query"]
            self.queries.append(q)
            m = re.fullmatch(r"select \* from (\w+) where Id in \((.*)\) maxresults 1000", q)
            entity, ids = m.group(1), re.findall(r"'(\d+)'", m.group(2))
            rows = [self.store[entity][i] for i in ids if i in self.store[entity]]
            return httpx.Response(200, json={"QueryResponse": {entity: rows} if rows else {}})
        m = re.fullmatch(r"(bill|vendorcredit)/(\d+)", path)
        if m:
            self.reads.append(path)
            if m.group(2) in self.deleted:
                return httpx.Response(400, json=fault("610", "Object Not Found",
                                                      "Something you're trying to use has been made inactive."))
            return httpx.Response(200, json={"Bill": {"Id": m.group(2)}})
        return httpx.Response(404)


def qbo_provider(org, fake):
    conn = QBOConnection.objects.get(organization=org)
    return QuickBooksProvider(conn, QBOClient(conn, http=httpx.Client(transport=httpx.MockTransport(fake.handler))))


def connect_qbo(org):
    return QBOConnection.objects.create(organization=org, realm_id="123", access_token="tok", refresh_token="ref",
                                        access_expires_at=timezone.now() + timedelta(hours=1), home_currency="USD",
                                        company_name="Sandbox Company_US_1", default_expense_account_id="77")


def make_bill(org, shipment, n, ext_id, *, system="quickbooks", ledger="123", kind="bill", total="100.00",
              currency="USD", **fields):
    doc = Document.objects.create(organization=org, original_filename=f"invoice-{n}.pdf", file=f"test/invoice-{n}.pdf",
                                  sha256=f"{n:064d}", status=Document.Status.MATCHED,
                                  doc_type="credit_note" if kind == "vendor_credit" else "freight_invoice")
    MatchLink.objects.create(document=doc, shipment=shipment, method="manual", score=1)
    for name, value in (("total_amount", total), ("currency", currency), ("invoice_number", f"INV-{n}"),
                        ("vendor_name", "Harborlink Logistics LLC")):
        ExtractedField.objects.create(document=doc, name=name, value=value, confidence=1)
    return PostedBill.objects.create(
        organization=org, document=doc, shipment=shipment, request_id=f"sm-{n}", system=system, ledger_id=ledger,
        kind=kind, status=PostedBill.Status.POSTED, qbo_bill_id=ext_id, external_number=f"INV-{n}",
        response={"Id": ext_id}, posted_at=timezone.now(), currency=currency, **fields)


@pytest.fixture
def posted(org):
    return Shipment.objects.create(organization=org, status=Shipment.Status.POSTED, bl_number="OSLN1234567890")


# --------------------------------------------------------------------------- QuickBooks


@pytest.mark.django_db
def test_quickbooks_payment_statuses_in_batches(org, posted, alert_hooks, django_capture_on_commit_callbacks):
    _channel(org, events_=["accounting.bill_voided"])
    connect_qbo(org)
    paid = make_bill(org, posted, 1, "500")
    partly = make_bill(org, posted, 2, "501")
    gone = make_bill(org, posted, 3, "502")
    voided = make_bill(org, posted, 4, "503")
    late = make_bill(org, posted, 5, "504")
    credit = make_bill(org, posted, 6, "700", kind="vendor_credit", total="185.00")
    elsewhere = make_bill(org, posted, 7, "505", ledger="999")   # another QuickBooks company: not read
    fake = FakeQBOPayments(
        bills=[
            {"Id": "500", "TotalAmt": 100, "Balance": 0, "DueDate": "2026-09-30", "CurrencyRef": {"value": "USD"},
             "LinkedTxn": [{"TxnId": "900", "TxnType": "BillPaymentCheck"}]},
            {"Id": "501", "TotalAmt": 100, "Balance": 60, "DueDate": "2026-12-31", "CurrencyRef": {"value": "USD"},
             "LinkedTxn": [{"TxnId": "901", "TxnType": "BillPaymentCreditCard"}]},
            {"Id": "503", "TotalAmt": 0, "Balance": 0, "PrivateNote": "Voided - ShipMatch SHP-000001"},
            {"Id": "504", "TotalAmt": 100, "Balance": 100, "DueDate": "2026-08-01", "LinkedTxn": []},
        ],
        bill_payments=[
            {"Id": "900", "TxnDate": "2026-09-28", "DocNumber": "CHK-1001", "Line": [
                {"Amount": 85, "LinkedTxn": [{"TxnId": "500", "TxnType": "Bill"}]},
                {"Amount": 15, "LinkedTxn": [{"TxnId": "700", "TxnType": "VendorCredit"}]}]},
            {"Id": "901", "TxnDate": "2026-09-15", "Line": [{"Amount": 40, "LinkedTxn": [{"TxnId": "501", "TxnType": "Bill"}]}]},
        ],
        credits=[{"Id": "700", "TotalAmt": 185, "Balance": 0, "CurrencyRef": {"value": "USD"}}],
        deleted={"502"})
    with django_capture_on_commit_callbacks(execute=True):
        summary = payments.sync(org, provider=qbo_provider(org, fake))
    assert summary["checked"] == 6 and summary["gone"] == 2 and summary["error"] == ""
    assert [q.split(" where")[0] for q in fake.queries] == ["select * from Bill", "select * from BillPayment",
                                                            "select * from VendorCredit"]   # one call per kind
    assert "'505'" not in fake.queries[0] and fake.reads == ["bill/502"]
    for pb in (paid, partly, gone, voided, late, credit, elsewhere):
        pb.refresh_from_db()
    assert paid.payment_status == P.PAID and paid.paid_on == date(2026, 9, 28) and paid.amount_paid == Decimal("100.00")
    assert paid.payments == [{"date": "2026-09-28", "amount": "85.00", "kind": "payment", "reference": "CHK-1001"},
                             {"date": "2026-09-28", "amount": "15.00", "kind": "credit", "reference": "vendor credit 700"}]
    assert partly.payment_status == P.PARTLY_PAID and partly.amount_due == Decimal("60.00") and partly.paid_on is None
    assert gone.payment_status == P.DELETED and voided.payment_status == P.VOIDED and gone.is_gone
    assert late.payment_status == P.UNPAID and late.is_overdue and late.payment_label == "Overdue"
    assert credit.payment_status == P.PAID and credit.payment_label == "Used in full"
    assert elsewhere.payment_status == "" and elsewhere.payment_checked_at is None
    assert AuditEvent.objects.filter(action="bill.voided_in_accounting").count() == 2
    # The first check finding an unpaid bill isn't news; every other change is in the audit log.
    assert AuditEvent.objects.filter(action="bill.payment_updated").count() == 5
    assert Delivery.objects.filter(event="accounting.bill_voided").count() == 2
    titles = sorted(r["blocks"][0]["text"]["text"] for r in (alert_hooks.json(i) for i in range(2)))
    assert titles == ["A bill was deleted in QuickBooks", "A bill was voided in QuickBooks"]
    # Nothing changed: no new audit rows or alerts on the next check.
    PaymentSync.objects.filter(organization=org).update(last_started_at=timezone.now() - timedelta(minutes=5))
    with django_capture_on_commit_callbacks(execute=True):
        again = payments.sync(org, provider=qbo_provider(org, fake))
    assert again["changed"] == 0 and Delivery.objects.filter(event="accounting.bill_voided").count() == 2


# --------------------------------------------------------------------------- Xero


@pytest.mark.django_db
def test_xero_payment_statuses_in_batches(org, posted, settings):
    settings.XERO_CLIENT_ID = "x"
    connect_xero(org)
    ids = {k: f"{k:08d}-1111-2222-3333-444444444444" for k in range(1, 8)}
    rows = {n: make_bill(org, posted, n, ids[n], system="xero", ledger=TENANT) for n in range(1, 6)}
    missing = make_bill(org, posted, 6, ids[6], system="xero", ledger=TENANT)
    credit = make_bill(org, posted, 7, ids[7], system="xero", ledger=TENANT, kind="vendor_credit", total="185.00")
    fake = FakeXero()
    paid_ms = int(datetime(2026, 9, 20, tzinfo=dt_timezone.utc).timestamp() * 1000)
    fake.invoices = {
        ids[1]: {"InvoiceID": ids[1], "Status": "PAID", "Total": 100, "AmountDue": 0, "AmountPaid": 100,
                 "AmountCredited": 0, "CurrencyCode": "USD", "DueDate": f"/Date({paid_ms}+0000)/",
                 "FullyPaidOnDate": f"/Date({paid_ms}+0000)/",
                 "Payments": [{"Date": f"/Date({paid_ms}+0000)/", "Amount": 100, "Reference": "EFT 4471"}]},
        ids[2]: {"InvoiceID": ids[2], "Status": "AUTHORISED", "Total": 100, "AmountDue": 45, "AmountPaid": 40,
                 "AmountCredited": 15, "CurrencyCode": "USD", "DueDateString": "2026-09-01T00:00:00",
                 "CreditNotes": [{"CreditNoteNumber": "CN-9", "AppliedAmount": 15}]},
        ids[3]: {"InvoiceID": ids[3], "Status": "VOIDED", "Total": 100, "AmountDue": 0, "CurrencyCode": "USD"},
        ids[4]: {"InvoiceID": ids[4], "Status": "DELETED", "Total": 100, "AmountDue": 0, "CurrencyCode": "USD"},
        ids[5]: {"InvoiceID": ids[5], "Status": "DRAFT", "Total": 100, "AmountDue": 100, "AmountPaid": 0,
                 "CurrencyCode": "USD", "DueDateString": "2099-01-01T00:00:00"},
    }
    fake.credit_notes = {ids[7]: {"CreditNoteID": ids[7], "Status": "AUTHORISED", "Total": 185, "RemainingCredit": 85,
                                  "CurrencyCode": "USD", "Allocations": [
                                      {"Amount": 100, "Date": "/Date(1790000000000+0000)/",
                                       "Invoice": {"InvoiceNumber": "INV-1"}}]}}
    summary = payments.sync(org, provider=XeroProvider(XeroConnection.objects.get(organization=org),
                                                       client_for(org, fake)))
    assert summary["checked"] == 7 and summary["error"] == ""
    invoice_calls = fake.calls("GET", "Invoices")
    assert len(invoice_calls) == 1 and len(invoice_calls[0][2]["IDs"].split(",")) == 6
    assert len(fake.calls("GET", "CreditNotes")) == 1
    for pb in (*rows.values(), missing, credit):
        pb.refresh_from_db()
    assert rows[1].payment_status == P.PAID and rows[1].paid_on == date(2026, 9, 20)
    assert rows[1].payments == [{"date": "2026-09-20", "amount": "100.00", "kind": "payment", "reference": "EFT 4471"}]
    assert rows[2].payment_status == P.PARTLY_PAID and rows[2].amount_paid == Decimal("55.00")
    assert rows[2].amount_due == Decimal("45.00") and rows[2].is_overdue and rows[2].payment_label == "Partly paid, overdue"
    assert rows[3].payment_status == P.VOIDED and rows[4].payment_status == P.DELETED
    assert rows[5].payment_status == P.UNPAID and not rows[5].is_overdue
    assert missing.payment_status == P.DELETED   # Xero keeps voided and deleted bills; a missing one is gone
    assert credit.payment_status == P.PARTLY_PAID and credit.amount_due == Decimal("85.00")
    assert credit.payment_label == "Partly used" and credit.payments[0]["reference"] == "INV-1"
    assert AuditEvent.objects.filter(action="bill.voided_in_accounting").count() == 3
    from apps.shipments.labels import describe_action

    event = AuditEvent.objects.filter(action="bill.payment_updated", data__status="paid").first()
    assert describe_action(event.action, event.data) == "Xero shows bill INV-1 as paid"


@pytest.mark.django_db
def test_daily_limit_pauses_the_checks(org, posted, settings):
    connect_xero(org)
    make_bill(org, posted, 1, "00000001-1111-2222-3333-444444444444", system="xero", ledger=TENANT)
    fake = FakeXero()
    fake.throttles = [("day", "7200")]
    provider = XeroProvider(XeroConnection.objects.get(organization=org), client_for(org, fake))
    summary = payments.sync(org, automatic=True, provider=provider)
    assert "daily limit" in summary["error"]
    state = PaymentSync.objects.get(organization=org)
    assert state.paused_until > timezone.now() + timedelta(hours=1) and state.running_since is None
    assert payments.sync(org, provider=provider)["skipped"] == "paused"


# --------------------------------------------------------------------------- limits


@pytest.mark.django_db
def test_automatic_checks_at_most_hourly_and_one_at_a_time(org, posted):
    connect_qbo(org)
    make_bill(org, posted, 1, "500")
    fake = FakeQBOPayments(bills=[{"Id": "500", "TotalAmt": 100, "Balance": 100}])
    assert payments.sync(org, automatic=True, provider=qbo_provider(org, fake))["checked"] == 1
    assert payments.sync(org, automatic=True, provider=qbo_provider(org, fake))["skipped"] == "recent"
    assert payments.sync(org, provider=qbo_provider(org, fake))["skipped"] == "just_checked"
    assert len(fake.queries) == 1
    state = PaymentSync.objects.get(organization=org)
    PaymentSync.objects.filter(pk=state.pk).update(last_auto_at=state.last_auto_at - timedelta(minutes=61),
                                                   last_started_at=state.last_started_at - timedelta(minutes=61),
                                                   running_since=timezone.now())
    assert payments.sync(org, automatic=True, provider=qbo_provider(org, fake))["skipped"] == "running"
    PaymentSync.objects.filter(pk=state.pk).update(running_since=timezone.now() - timedelta(minutes=20))  # died
    assert payments.sync(org, automatic=True, provider=qbo_provider(org, fake))["checked"] == 1


@pytest.mark.django_db
def test_paid_bills_are_read_again_weekly_for_90_days(org, posted):
    connect_qbo(org)
    now = timezone.now()
    recent = make_bill(org, posted, 1, "500", payment_status=P.PAID, payment_checked_at=now - timedelta(days=1),
                       payment_changed_at=now - timedelta(days=10))
    due = make_bill(org, posted, 2, "501", payment_status=P.PAID, payment_checked_at=now - timedelta(days=8),
                    payment_changed_at=now - timedelta(days=10))
    old = make_bill(org, posted, 3, "502", payment_status=P.PAID, payment_checked_at=now - timedelta(days=8),
                    payment_changed_at=now - timedelta(days=100))
    gone = make_bill(org, posted, 4, "503", payment_status=P.DELETED)
    provider = qbo_provider(org, FakeQBOPayments())
    picked = set(payments.candidates(org, provider).values_list("pk", flat=True))
    assert picked == {due.pk} and recent.pk not in picked and old.pk not in picked and gone.pk not in picked
    assert set(payments.candidates(org, provider, shipment=posted).values_list("pk", flat=True)) == {
        recent.pk, due.pk, old.pk}


@pytest.mark.django_db
def test_beat_task_reads_connected_organizations(org, settings, monkeypatch):
    from apps.core.models import Organization

    other = Organization.objects.create(name="Other", slug="other")
    broken = Organization.objects.create(name="Broken", slug="broken")
    connect_qbo(org)
    connect_xero(other)
    XeroConnection.objects.create(organization=broken, tenant_id=TENANT, access_token="a", refresh_token="r",
                                  access_expires_at=timezone.now(), needs_reconnect=True)
    calls = []
    monkeypatch.setattr(payments, "sync", lambda o, **kw: calls.append((o.slug, kw)) or {})
    assert tasks.sync_all_payments() == 2
    assert sorted(calls) == [("other", {"automatic": True, "actor": None, "shipment": None}),
                             ("test", {"automatic": True, "actor": None, "shipment": None})]
    settings.PAYMENT_SYNC_ENABLED = False
    assert tasks.sync_all_payments() == 0
    assert "accounting-payment-status" in settings.CELERY_BEAT_SCHEDULE


# --------------------------------------------------------------------------- pages


@pytest.fixture
def qbo_http(monkeypatch):
    """QBOClient opened by the views talks to a fake that answers bill 500 as paid."""
    fake = FakeQBOPayments(bills=[{"Id": "500", "TotalAmt": 100, "Balance": 0, "LinkedTxn": [
        {"TxnId": "900", "TxnType": "BillPaymentCheck"}]}], bill_payments=[
        {"Id": "900", "TxnDate": "2026-09-28", "Line": [{"Amount": 100, "LinkedTxn": [{"TxnId": "500", "TxnType": "Bill"}]}]}])
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real(transport=httpx.MockTransport(fake.handler)))
    return fake


@pytest.mark.django_db
def test_check_payments_now_button(client, org, posted, approver, user, viewer, qbo_http, settings, monkeypatch):
    url = reverse("accounting:check_payments")
    client.force_login(approver)
    r = client.post(url, follow=True)
    assert "Connect QuickBooks or Xero" in r.content.decode()
    connect_qbo(org)
    pb = make_bill(org, posted, 1, "500")
    r = client.post(url, {"shipment": posted.pk, "next": "https://evil.example/"})
    assert r.url == reverse("review:shipment", args=[posted.pk])   # never redirects off-site
    page = client.get(r.url).content.decode()
    assert "Checked 1 bill in QuickBooks. 1 changed status." in page
    pb.refresh_from_db()
    assert pb.payment_status == P.PAID and pb.paid_on == date(2026, 9, 28)
    assert AuditEvent.objects.get(action="accounting.payments_checked").actor == approver
    for who in (user, viewer):   # reviewers and viewers see payments but don't run checks
        client.force_login(who)
        assert client.post(url).status_code == 403
    client.force_login(approver)
    settings.CELERY_TASK_ALWAYS_EAGER = False
    queued = []
    monkeypatch.setattr(tasks.sync_org_payments, "delay", lambda *a, **kw: queued.append((a, kw)))
    r = client.post(url, follow=True)
    assert queued == [((org.pk,), {"automatic": False, "user_id": approver.pk, "shipment_id": None})]
    assert "Checking payments in QuickBooks" in r.content.decode()


@pytest.mark.django_db
def test_shipment_page_shows_payments_and_warns_about_deleted_bills(client, org, posted, approver, viewer):
    connect_qbo(org)
    make_bill(org, posted, 1, "500", payment_status=P.PAID, paid_on=date(2026, 9, 28), amount_total=Decimal("100.00"),
              amount_paid=Decimal("100.00"), amount_due=Decimal("0.00"), payment_checked_at=timezone.now(),
              payments=[{"date": "2026-09-28", "amount": "100.00", "kind": "payment", "reference": "CHK-1001"}])
    make_bill(org, posted, 2, "501", payment_status=P.DELETED, amount_total=Decimal("250.00"))
    make_bill(org, posted, 3, "502", payment_status=P.UNPAID, due_date=timezone.localdate() - timedelta(days=12),
              amount_total=Decimal("75.00"), amount_due=Decimal("75.00"))
    client.force_login(approver)
    page = client.get(reverse("review:shipment", args=[posted.pk])).content.decode()
    assert "Payments" in page and "Paid on 28 Sep 2026" in page and "CHK-1001" in page
    assert "Bill INV-2 was deleted in QuickBooks." in page and "12 days late" in page
    assert "Check payments now" in page and "app.sandbox.qbo.intuit.com/app/bill?txnId=500" in page
    assert "QuickBooks: Posted, bill INV-1" in page
    client.force_login(viewer)
    page = client.get(reverse("review:shipment", args=[posted.pk])).content.decode()
    assert "Paid on 28 Sep 2026" in page and "Check payments now" not in page


@pytest.mark.django_db
def test_shipment_list_payment_column_and_filter(client, org, user):
    connect_qbo(org)
    today = timezone.localdate()
    late, paid, gone, open_, never = (Shipment.objects.create(organization=org, status=Shipment.Status.POSTED)
                                      for _ in range(5))
    empty = Shipment.objects.create(organization=org, status=Shipment.Status.APPROVED)
    make_bill(org, late, 1, "500", payment_status=P.UNPAID, due_date=today - timedelta(days=3))
    make_bill(org, late, 2, "501", payment_status=P.PAID)
    make_bill(org, paid, 3, "502", payment_status=P.PAID)
    make_bill(org, gone, 4, "503", payment_status=P.VOIDED)
    make_bill(org, open_, 5, "504", payment_status=P.PARTLY_PAID, due_date=today + timedelta(days=3))
    make_bill(org, never, 6, "505")
    client.force_login(user)
    url = reverse("review:queue")
    page = client.get(url, {"status": "all"}).content.decode()
    assert "<th>Payments</th>" in page and 'name="pay"' in page
    for label in ("Overdue", "Paid", "Voided or deleted", "Partly paid", "Not checked yet"):
        assert f">{label}</span>" in page

    def refs(pay):
        html = client.get(url, {"status": "all", "pay": pay}).content.decode()
        return {s.reference for s in (late, paid, gone, open_, never, empty) if f">{s.reference}</a>" in html}

    assert refs("overdue") == {late.reference}
    assert refs("unpaid") == {late.reference, open_.reference}
    assert refs("paid") == {paid.reference}
    assert refs("problem") == {gone.reference}
    assert refs("not_checked") == {never.reference}
    assert len(refs("")) == 6


@pytest.mark.django_db
def test_dashboard_aging_summary(client, org, posted, viewer):
    org.fx_rates = {"GBP": "1.25"}
    org.save()
    connect_qbo(org)
    today = timezone.localdate()
    for n, days, amount in ((1, -5, "100.00"), (2, 10, "200.00"), (3, 45, "300.00"), (4, 75, "400.00"),
                            (5, 120, "500.00")):
        make_bill(org, posted, n, str(500 + n), payment_status=P.UNPAID, due_date=today - timedelta(days=days),
                  amount_total=Decimal(amount), amount_due=Decimal(amount))
    make_bill(org, posted, 6, "506", payment_status=P.PARTLY_PAID, due_date=today - timedelta(days=1),
              amount_total=Decimal("1000.00"), amount_due=Decimal("40.00"), currency="GBP")
    make_bill(org, posted, 7, "507", payment_status=P.UNPAID, amount_due=Decimal("80.00"), currency="EUR")
    make_bill(org, posted, 8, "508", payment_status=P.PAID, amount_due=Decimal("0.00"))
    make_bill(org, posted, 9, "700", kind="vendor_credit", payment_status=P.PARTLY_PAID, amount_due=Decimal("85.00"))
    make_bill(org, posted, 10, "509")   # posted, never checked
    aging = payments.aging(org)
    by = {b.key: (b.count, b.amount) for b in aging.buckets}
    assert by == {"current": (1, Decimal("100.00")), "1_30": (2, Decimal("250.00")), "31_60": (1, Decimal("300.00")),
                  "61_90": (1, Decimal("400.00")), "90_plus": (1, Decimal("500.00"))}
    assert aging.total == Decimal("1550.00") and aging.overdue == Decimal("1450.00")
    assert aging.unconverted == {"EUR": Decimal("80.00")} and aging.credits_open == {"USD": Decimal("85.00")}
    assert aging.not_checked == 1
    client.force_login(viewer)
    page = client.get(reverse("core:dashboard")).content.decode()
    assert "Unpaid bills by age" in page and "Over 90 days" in page and "USD 1,550.00" in page
    assert "EUR 80.00" in page and "no exchange rate" in page and "Vendor credits not used yet: USD 85.00" in page
    assert "QuickBooks connected: Sandbox Company_US_1" in page and "Check payments now" not in page
    assert f"{reverse('review:queue')}?status=all&amp;pay=overdue" in page


@pytest.mark.django_db
def test_dashboard_without_accounting_shows_no_aging(client, org, viewer):
    client.force_login(viewer)
    page = client.get(reverse("core:dashboard")).content.decode()
    assert "Unpaid bills by age" not in page and "No accounting system connected" in page
