"""Features from different branches working together: credit notes close disputes, and money
recovered through disputes shows on the Savings page."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.disputes.models import Dispute
from apps.disputes.services import sending, workflow
from apps.documents.services.ingest import ingest_bytes
from apps.rates import savings as rate_savings
from apps.shipments.models import Shipment
from synthetic.extra import credit_note
from synthetic.generator import Party


@pytest.fixture
def sent(org, dataset, user, approver):
    for f in ["S03_1_commercial_invoice.pdf", "S03_2_bill_of_lading.pdf", "S03_3_freight_invoice.pdf"]:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    shipment = Shipment.objects.get(organization=org, issues__code="total_mismatch")
    issue = shipment.issues.get(code="total_mismatch")
    dispute, _ = workflow.create_draft(shipment, issue.document, [issue.pk], user, use_ai=False)
    dispute.vendor_email = "billing@vendor.example"
    dispute.save(update_fields=["vendor_email"])
    sending.send(dispute, approver)
    dispute.refresh_from_db()
    return dispute


def _credit(org, dispute, amount: str, original: str | None, name="credit.pdf", currency="USD"):
    vendor = Party(dispute.invoice.field("vendor_name"), "1 Quay Road", "vendor.example")
    pdf, _ = credit_note("CN-7001", original, vendor=vendor, charges=[("Overcharge refund", amount)],
                         currency=currency)
    return ingest_bytes(org, name, pdf, process="sync")[0]


@pytest.mark.django_db(transaction=True)
def test_credit_note_naming_the_disputed_invoice_is_suggested_not_applied(client, org, sent, approver):
    assert sent.status == Dispute.Status.SENT and sent.amount_disputed == Decimal("100.00")
    doc = _credit(org, sent, "100.00", sent.invoice.field("invoice_number"))
    doc.refresh_from_db()
    assert doc.doc_type == "credit_note"
    sent.refresh_from_db()
    # Nothing is recorded or released until an approver confirms: anyone can email a credit note in.
    assert sent.status == Dispute.Status.SENT and sent.credit_note_id is None
    assert sent.items.filter(issue__resolved=False).exists()
    assert sent.events.filter(data__suggested_credit=doc.pk).count() == 1
    client.force_login(approver)
    page = client.get(reverse("disputes:detail", args=[sent.pk])).content.decode()
    assert "looks like the credit for this dispute" in page
    assert f'<option value="{doc.pk}" selected>' in page and 'value="100.00"' in page
    client.post(reverse("disputes:credit", args=[sent.pk]), {"amount": "100.00", "credit_note": doc.pk, "settles": "on"})
    sent.refresh_from_db()
    assert sent.status == Dispute.Status.RESOLVED and sent.credit_note_id == doc.pk
    assert not sent.items.filter(issue__resolved=False).exists()


@pytest.mark.django_db(transaction=True)
def test_credit_note_for_another_invoice_is_left_alone(org, sent):
    _credit(org, sent, "100.00", "SOME-OTHER-INVOICE")
    sent.refresh_from_db()
    assert sent.status == Dispute.Status.SENT and not sent.events.filter(data__has_key="suggested_credit").exists()


@pytest.mark.django_db(transaction=True)
def test_recovered_money_shows_on_the_savings_page(org, sent, approver):
    doc = _credit(org, sent, "100.00", sent.invoice.field("invoice_number"))
    workflow.record_credit(sent, approver, "100.00", str(doc.pk), settles=True)
    today = timezone.localdate()
    s = rate_savings.summary(org, today - timedelta(days=30), today)
    assert s.recovery_sources >= 1
    assert s.recovered == Decimal("100.00")
    assert any("credit from" in r.label for r in s.recoveries)


@pytest.mark.django_db
def test_mailbox_and_vendor_credit_failures_raise_alerts(org):
    from apps.core.utils import audit
    from apps.mailboxes.models import Mailbox
    from apps.notifications import events

    box = Mailbox.objects.create(organization=org, kind=Mailbox.Kind.IMAP, display_name="AP inbox")
    e = audit(org, "mailbox.needs_reconnect", box, name="AP inbox", error="invalid_grant")
    msg = events.audit_message(events.AUDIT_EVENTS[e.action], e)
    assert msg.event == events.MAILBOX_RECONNECT and "AP inbox" in msg.text and msg.url.endswith("/settings/email/")
    assert events.AUDIT_EVENTS["vendor_credit.failed"] == events.BILL_FAILED
