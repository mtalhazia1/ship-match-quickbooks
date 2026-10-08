"""Disputes: drafting from a shipment's issues, sending with the invoice attached, the status flow,
how an open dispute holds the shipment, permissions, overdue flags and recovered totals."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from smtplib import SMTPException

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuditEvent
from apps.disputes import savings
from apps.disputes.models import Dispute, DisputeEvent, DisputeSettings, VendorContact
from apps.disputes.services import drafting, sending, workflow
from apps.disputes.services.workflow import DisputeError
from apps.disputes.tasks import flag_overdue
from apps.documents.models import Document, ExtractedField
from apps.documents.services import llm
from apps.documents.services.ingest import ingest_bytes
from apps.shipments.models import Shipment, ValidationIssue
from apps.shipments.services.approval import approval_blockers
from apps.shipments.services.validation import validate_shipment


@pytest.fixture
def loaded(org, dataset):
    for f in ["S03_1_commercial_invoice.pdf", "S03_2_bill_of_lading.pdf", "S03_3_freight_invoice.pdf"]:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    return org


@pytest.fixture
def bad(loaded):
    """The shipment whose freight invoice total is USD 100.00 more than its lines."""
    return Shipment.objects.get(organization=loaded, issues__code="total_mismatch")


@pytest.fixture
def issue(bad):
    return bad.issues.get(code="total_mismatch")


def _draft(bad, issue, user):
    dispute, _ = workflow.create_draft(bad, issue.document, [issue.pk], user, use_ai=False)
    return dispute


def _send(dispute, approver, email="billing@atlas.example"):
    dispute.vendor_email = email
    dispute.save(update_fields=["vendor_email"])
    sending.send(dispute, approver)
    dispute.refresh_from_db()
    return dispute


# ---------------------------------------------------------------- drafting


@pytest.mark.django_db
def test_shipment_page_offers_dispute_with_money_issue_preselected(client, user, bad, issue):
    client.force_login(user)
    html = client.get(reverse("review:shipment", args=[bad.pk])).content.decode()
    assert "Dispute with vendor" in html
    assert f'name="issues" value="{issue.pk}" checked' in html
    # Checks about our own paperwork are never offered to the vendor.
    for i in bad.issues.filter(code__in=["low_confidence", "fuzzy_match", "missing_bl"]):
        assert f'name="issues" value="{i.pk}"' not in html


@pytest.mark.django_db
def test_draft_lists_the_evidence_in_plain_words(client, user, bad, issue):
    client.force_login(user)
    r = client.post(reverse("disputes:create", args=[bad.pk]), {"invoice": issue.document_id, "issues": [issue.pk]})
    dispute = Dispute.objects.get(organization=bad.organization)
    assert r.status_code == 302 and r["Location"] == reverse("disputes:edit", args=[dispute.pk])
    inv = issue.document.data()
    assert dispute.status == Dispute.Status.DRAFT
    assert dispute.amount_disputed == Decimal("100.00") == issue.amount_at_risk
    assert dispute.vendor_name == inv["vendor_name"] and dispute.invoice_number == inv["invoice_number"]
    body = dispute.body
    for text in (inv["invoice_number"], str(inv["invoice_date"]), bad.bl_number, *bad.container_numbers,
                 "USD 100.00", dispute.reference, bad.reference, "credit note", "corrected invoice"):
        assert text in body, text
    assert f"{Decimal(issue.data['line_sum']):,.2f}" in body and f"{Decimal(issue.data['total']):,.2f}" in body
    assert "USD 100.00" in dispute.subject and dispute.reference in dispute.subject
    assert dispute.items.get().issue_id == issue.pk
    assert DisputeEvent.objects.filter(dispute=dispute, kind="created").exists()
    assert AuditEvent.objects.filter(action="dispute.drafted", object_id=str(dispute.pk)).exists()
    # Same evidence, same words.
    assert drafting.compose(dispute) == (dispute.subject, dispute.body)


@pytest.mark.django_db
def test_quote_reference_from_issue_data_is_quoted(bad, issue, user):
    issue.data = {**issue.data, "quote_ref": "Q-2026-0042"}
    issue.save(update_fields=["data"])
    dispute = _draft(bad, issue, user)
    assert "Your quote: Q-2026-0042" in dispute.body


@pytest.mark.django_db
def test_second_open_dispute_for_same_invoice_is_refused(bad, issue, user):
    first = _draft(bad, issue, user)
    with pytest.raises(DisputeError, match=first.reference):
        workflow.create_draft(bad, issue.document, [issue.pk], user, use_ai=False)


@pytest.mark.django_db
def test_editing_issues_rewrites_untouched_text_and_amount(client, user, bad, issue):
    dispute = _draft(bad, issue, user)
    client.force_login(user)
    other = ValidationIssue.objects.create(
        organization=bad.organization, shipment=bad, document=issue.document, code="amount_outlier",
        severity="warning", message="x.pdf: 9,000.00 per container vs typical 3,000.00", fingerprint="amount_outlier:x",
        data={"per_container": "9000.00", "typical": "3000.00"}, amount_at_risk=Decimal("6000.00"), currency="USD")
    client.post(reverse("disputes:edit", args=[dispute.pk]), {
        "action": "save", "vendor_email": "ar@atlas.example", "contact_name": "Dana Reyes", "cc": "",
        "subject": dispute.subject, "body": dispute.body, "amount": str(dispute.amount_disputed),
        "issues": [issue.pk, other.pk], "follow_up_on": "", "remember": "on"})
    dispute.refresh_from_db()
    assert dispute.amount_disputed == Decimal("6100.00")
    assert "USD 6,100.00" in dispute.body and "USD 9,000.00 per container" in dispute.body
    assert dispute.body.startswith("Hello Dana,")
    assert VendorContact.objects.get(organization=bad.organization, vendor_key=dispute.vendor_key).email == "ar@atlas.example"

    # Edited text is kept when the facts change; the person is told how to refresh it.
    r = client.post(reverse("disputes:edit", args=[dispute.pk]), {
        "action": "save", "vendor_email": "ar@atlas.example", "subject": "My subject", "body": "My own words",
        "amount": "50", "issues": [issue.pk]}, follow=True)
    dispute.refresh_from_db()
    assert dispute.body == "My own words" and dispute.amount_disputed == Decimal("50.00")
    assert "your edited text was kept" in r.content.decode()


@pytest.mark.django_db
def test_issue_relinked_after_shipment_is_checked_again(bad, issue, user):
    dispute = _draft(bad, issue, user)
    validate_shipment(bad)  # open issues are deleted and re-created
    assert not ValidationIssue.objects.filter(pk=issue.pk).exists()
    item = dispute.items.get()
    new = bad.issues.get(code="total_mismatch")
    assert item.issue_id == new.pk and item.fingerprint == new.fingerprint


@pytest.mark.django_db
def test_ai_polish_is_used_only_when_it_keeps_every_fact(bad, issue, user, monkeypatch):
    monkeypatch.setattr(llm, "is_enabled", lambda: True)
    calls = []

    def good(system, user_text, schema, **kw):
        calls.append((schema, kw))
        d = Dispute.objects.order_by("-pk").first()
        keep = " ".join(drafting.must_keep(d))
        return {"subject": f"Invoice query {d.reference}", "body": f"Dear team, kindly review: {keep}. Regards"}

    monkeypatch.setattr(llm, "structured_call", good)
    dispute, note = workflow.create_draft(bad, issue.document, [issue.pk], user, use_ai=True)
    assert dispute.ai_polished and dispute.body.startswith("Dear team") and "AI reworded" in note
    schema, kw = calls[0]
    assert schema["additionalProperties"] is False and kw["purpose"] == "dispute_polish"

    workflow.discard(dispute, user)
    monkeypatch.setattr(llm, "structured_call", lambda *a, **k: {"subject": "Hi", "body": "Please fix the invoice."})
    dispute, note = workflow.create_draft(bad, issue.document, [issue.pk], user, use_ai=True)
    assert not dispute.ai_polished and "USD 100.00" in dispute.body and "left out" in note

    workflow.discard(dispute, user)

    def broken(*a, **k):
        raise llm.LLMError("timeout")

    monkeypatch.setattr(llm, "structured_call", broken)
    dispute, note = workflow.create_draft(bad, issue.document, [issue.pk], user, use_ai=True)
    assert not dispute.ai_polished and "standard wording was kept" in note


# ---------------------------------------------------------------- sending


@pytest.mark.django_db
def test_approver_sends_with_invoice_attached_and_reply_to(client, user, approver, bad, issue, mailoutbox):
    DisputeSettings.objects.create(organization=bad.organization, reply_to="ap@testimports.example",
                                   signature="Kim Lee\nAccounts payable, Test Imports")
    dispute = _draft(bad, issue, user)
    client.force_login(approver)
    r = client.post(reverse("disputes:edit", args=[dispute.pk]), {
        "action": "send", "vendor_email": "billing@atlas.example", "contact_name": "", "cc": "ops@atlas.example",
        "subject": dispute.subject, "body": dispute.body, "amount": "100.00", "issues": [issue.pk], "remember": "on"})
    dispute.refresh_from_db()
    assert r.status_code == 302 and r["Location"] == reverse("disputes:detail", args=[dispute.pk])
    assert dispute.status == Dispute.Status.SENT and dispute.sent_by == approver and dispute.sent_at
    assert len(mailoutbox) == 1
    m = mailoutbox[0]
    assert m.to == ["billing@atlas.example"] and m.cc == ["ops@atlas.example"]
    assert m.reply_to == ["ap@testimports.example"] and m.bcc == ["ap@testimports.example"]
    assert m.subject == dispute.subject and "Kim Lee" in m.body
    name, content, mimetype = m.attachments[0]
    assert name == issue.document.original_filename and mimetype == "application/pdf"
    assert content.startswith(b"%PDF")
    assert m.extra_headers["Message-ID"] == dispute.message_id and dispute.reference in dispute.message_id
    assert m.message()["Message-ID"] == dispute.message_id
    assert dispute.follow_up_on == timezone.localdate() + timedelta(days=7)
    assert VendorContact.objects.filter(organization=bad.organization, email="billing@atlas.example").exists()
    assert AuditEvent.objects.filter(action="dispute.sent", actor=approver).exists()
    # Sending twice is refused; the vendor gets one email.
    with pytest.raises(DisputeError):
        sending.send(dispute, approver)
    assert len(mailoutbox) == 1


@pytest.mark.django_db
def test_reviewer_drafts_but_cannot_send(client, user, viewer, bad, issue, mailoutbox):
    client.force_login(viewer)
    assert client.post(reverse("disputes:create", args=[bad.pk]), {"invoice": issue.document_id,
                                                                   "issues": [issue.pk]}).status_code == 403
    assert client.get(reverse("disputes:list")).status_code == 200

    client.force_login(user)
    client.post(reverse("disputes:create", args=[bad.pk]), {"invoice": issue.document_id, "issues": [issue.pk]})
    dispute = Dispute.objects.get()
    r = client.post(reverse("disputes:edit", args=[dispute.pk]), {
        "action": "send", "vendor_email": "billing@atlas.example", "subject": dispute.subject, "body": dispute.body,
        "amount": "100", "issues": [issue.pk]}, follow=True)
    assert "Only approvers and admins can send" in r.content.decode()
    assert client.post(reverse("disputes:send", args=[dispute.pk])).status_code == 403
    dispute.refresh_from_db()
    assert dispute.status == Dispute.Status.DRAFT and dispute.vendor_email == "billing@atlas.example"
    assert mailoutbox == []
    # Viewers can read a dispute but not change it.
    client.force_login(viewer)
    assert client.get(reverse("disputes:detail", args=[dispute.pk])).status_code == 200
    assert client.get(reverse("disputes:edit", args=[dispute.pk])).status_code == 403
    assert client.post(reverse("disputes:note", args=[dispute.pk]), {"text": "hi"}).status_code == 403


@pytest.mark.django_db
def test_send_needs_address_and_reports_mail_server_errors(approver, user, bad, issue, mailoutbox, monkeypatch):
    dispute = _draft(bad, issue, user)
    with pytest.raises(DisputeError, match="vendor's email address"):
        sending.send(dispute, approver)

    def refuse(self, fail_silently=False):
        raise SMTPException("550 relay denied")

    monkeypatch.setattr("django.core.mail.EmailMessage.send", refuse)
    dispute.vendor_email = "billing@atlas.example"
    dispute.save()
    with pytest.raises(DisputeError, match="550 relay denied"):
        sending.send(dispute, approver)
    dispute.refresh_from_db()
    assert dispute.status == Dispute.Status.DRAFT and not dispute.message_id
    assert dispute.events.filter(kind="send_failed").exists()
    assert AuditEvent.objects.filter(action="dispute.send_failed").exists()


@pytest.mark.django_db
def test_other_organization_cannot_open_or_start_disputes(client, bad, issue, user, django_user_model):
    dispute = _draft(bad, issue, user)
    stranger = django_user_model.objects.create_user("stranger", password="pw-123456789")
    client.force_login(stranger)
    assert client.get(reverse("disputes:detail", args=[dispute.pk])).status_code == 404
    assert client.post(reverse("disputes:create", args=[bad.pk]), {"invoice": issue.document_id}).status_code == 404


# ---------------------------------------------------------------- status flow and the shipment


@pytest.mark.django_db
def test_sent_dispute_holds_shipment_until_released(client, user, approver, approver2, bad, issue):
    client.force_login(user)
    for w in bad.issues.filter(resolved=False, severity="warning"):
        client.post(reverse("review:resolve_issue", args=[w.pk]))
    dispute = _send(_draft(bad, issue, user), approver)
    # Even with the error overridden, the waiting dispute blocks approval.
    client.force_login(approver)
    client.post(reverse("review:resolve_issue", args=[issue.pk]), {"note": "Checked the lines by hand."})
    reasons = approval_blockers(bad, approver2)
    assert any(dispute.reference in r for r in reasons)
    client.force_login(approver2)
    client.post(reverse("review:approve", args=[bad.pk]))
    bad.refresh_from_db()
    assert bad.status != Shipment.Status.APPROVED
    assert dispute.reference in client.get(reverse("review:shipment", args=[bad.pk])).content.decode()

    # Release needs a real note.
    client.post(reverse("disputes:release", args=[dispute.pk]), {"note": "ok"})
    dispute.refresh_from_db()
    assert not dispute.hold_released
    client.post(reverse("disputes:release", args=[dispute.pk]),
                {"note": "Vendor confirmed by phone a credit note follows."})
    dispute.refresh_from_db()
    assert dispute.hold_released and dispute.status == Dispute.Status.SENT
    assert not any(dispute.reference in r for r in approval_blockers(bad, approver2))
    client.post(reverse("review:approve", args=[bad.pk]))
    bad.refresh_from_db()
    assert bad.status == Shipment.Status.APPROVED


@pytest.mark.django_db
def test_release_resolves_open_disputed_issue_in_approvers_name(user, approver, bad, issue):
    dispute = _send(_draft(bad, issue, user), approver)
    assert workflow.release_hold(dispute, approver, "Paying on time; credit expected next week.") == 1
    issue.refresh_from_db()
    assert issue.resolved and issue.resolved_by == approver and dispute.reference in issue.resolution_note
    assert AuditEvent.objects.filter(action="issue.resolved", object_id=str(issue.pk), actor=approver).exists()


@pytest.mark.django_db
def test_status_flow_reply_credit_resolve(client, user, approver, bad, issue):
    dispute = _send(_draft(bad, issue, user), approver)
    client.force_login(user)
    client.post(reverse("disputes:reply", args=[dispute.pk]), {"text": "We will issue a credit note.", "agreed": "on"})
    dispute.refresh_from_db()
    assert dispute.status == Dispute.Status.ACKNOWLEDGED
    # Reviewers can't record money; approvers can.
    assert client.post(reverse("disputes:credit", args=[dispute.pk]), {"amount": "100"}).status_code == 403
    client.force_login(approver)
    r = client.post(reverse("disputes:credit", args=[dispute.pk]), {"amount": "60.00", "note": "CN-1 partial"})
    assert r.status_code == 302
    dispute.refresh_from_db()
    assert dispute.status == Dispute.Status.CREDIT_RECEIVED and dispute.amount_recovered == Decimal("60.00")
    assert dispute.amount_recovered_home == Decimal("60.00") and dispute.outstanding == Decimal("40.00")
    issue.refresh_from_db()
    assert issue.resolved and dispute.reference in issue.resolution_note and issue.resolved_by == approver
    assert AuditEvent.objects.filter(action="dispute.credit_received", object_id=str(dispute.pk)).exists()
    # Closing is for disputes with nothing back; this one is resolved instead.
    with pytest.raises(DisputeError, match="resolved"):
        workflow.close(dispute, approver, "Vendor refused the rest.")
    client.post(reverse("disputes:resolve", args=[dispute.pk]), {"note": "Rest written off"})
    dispute.refresh_from_db()
    assert dispute.status == Dispute.Status.RESOLVED and dispute.closed_at
    kinds = list(dispute.events.values_list("kind", flat=True))
    assert {"created", "sent", "reply", "credit", "resolved"} <= set(kinds)
    # The timeline is on the page.
    assert "We will issue a credit note." in client.get(reverse("disputes:detail", args=[dispute.pk])).content.decode()


@pytest.mark.django_db
def test_credit_that_settles_resolves_in_one_step_and_links_document(approver, user, bad, issue, loaded):
    dispute = _send(_draft(bad, issue, user), approver)
    credit_doc = bad.documents.exclude(pk=issue.document_id).first()
    with pytest.raises(DisputeError, match="disputed invoice itself"):
        workflow.record_credit(dispute, approver, "100", str(issue.document_id))
    with pytest.raises(DisputeError):
        workflow.record_credit(dispute, approver, "0")
    workflow.record_credit(dispute, approver, "100.00", str(credit_doc.pk), settles=True)
    dispute.refresh_from_db()
    assert dispute.status == Dispute.Status.RESOLVED and dispute.credit_note == credit_doc


@pytest.mark.django_db
def test_close_without_recovery_needs_reason(approver, user, bad, issue):
    draft = _draft(bad, issue, user)
    with pytest.raises(DisputeError, match="Discard"):
        workflow.close(draft, approver, "Not worth chasing at all.")
    dispute = _send(draft, approver)
    with pytest.raises(DisputeError, match="at least"):
        workflow.close(dispute, approver, "no")
    workflow.close(dispute, approver, "Surcharge was in the signed contract.")
    dispute.refresh_from_db()
    issue.refresh_from_db()
    assert dispute.status == Dispute.Status.CLOSED and dispute.outcome_note.startswith("Surcharge")
    assert issue.resolved and "without recovery" in issue.resolution_note


@pytest.mark.django_db
def test_discard_draft_only(client, user, approver, bad, issue):
    dispute = _draft(bad, issue, user)
    client.force_login(user)
    client.post(reverse("disputes:discard", args=[dispute.pk]))
    assert not Dispute.objects.filter(pk=dispute.pk).exists()
    sent = _send(_draft(bad, issue, user), approver)
    with pytest.raises(DisputeError):
        workflow.discard(sent, user)


@pytest.mark.django_db
def test_dispute_after_approval_never_changes_the_locked_shipment(approver, user, bad, issue):
    dispute = _send(_draft(bad, issue, user), approver)
    Shipment.objects.filter(pk=bad.pk).update(status=Shipment.Status.APPROVED)
    dispute.shipment.refresh_from_db()
    workflow.record_credit(dispute, approver, "100", settles=True)
    issue.refresh_from_db()
    assert not issue.resolved  # read only: the shipment keeps its record as approved


# ---------------------------------------------------------------- follow-up and totals


@pytest.mark.django_db
def test_overdue_disputes_are_flagged_once_per_follow_up_date(client, user, approver, bad, issue):
    dispute = _send(_draft(bad, issue, user), approver)
    Dispute.objects.filter(pk=dispute.pk).update(follow_up_on=timezone.localdate() - timedelta(days=2))
    assert [d.pk for d in flag_overdue()] == [dispute.pk]
    assert flag_overdue() == []
    e = AuditEvent.objects.get(action="dispute.overdue")
    assert e.data["days_overdue"] == 2 and e.data["reference"] == dispute.reference
    assert DisputeEvent.objects.filter(dispute=dispute, kind="overdue").count() == 1
    client.force_login(user)
    html = client.get(reverse("disputes:list") + "?status=waiting&age=overdue").content.decode()
    assert dispute.reference in html and "Past follow-up date" in html
    # A new follow-up date that passes is flagged again.
    workflow.set_follow_up(Dispute.objects.get(pk=dispute.pk), user, timezone.localdate().isoformat())
    Dispute.objects.filter(pk=dispute.pk).update(follow_up_on=timezone.localdate() - timedelta(days=1))
    assert len(flag_overdue()) == 1
    # Not flagged once the vendor has answered with a credit.
    workflow.record_credit(Dispute.objects.get(pk=dispute.pk), approver, "100", settles=True)
    Dispute.objects.filter(pk=dispute.pk).update(follow_up_on=timezone.localdate() - timedelta(days=5))
    assert flag_overdue() == []


@pytest.mark.django_db
def test_follow_up_date_rules(user, approver, bad, issue):
    dispute = _send(_draft(bad, issue, user), approver)
    with pytest.raises(DisputeError, match="past"):
        workflow.set_follow_up(dispute, user, (timezone.localdate() - timedelta(days=1)).isoformat())
    with pytest.raises(DisputeError, match="date"):
        workflow.set_follow_up(dispute, user, "next tuesday")


def _recovered(org, amount, currency, when, status=Dispute.Status.RESOLVED, home=None):
    return Dispute.objects.create(organization=org, vendor_name="V", vendor_key="v", status=status,
                                  amount_disputed=amount, amount_recovered=amount, currency=currency,
                                  amount_recovered_home=home, recovered_at=when)


@pytest.mark.django_db
def test_recovered_totals_in_home_currency(org):
    org.fx_rates = {"EUR": "1.10"}
    org.save()
    tz = timezone.get_current_timezone()
    mid = datetime(2026, 9, 15, 12, tzinfo=tz)
    _recovered(org, Decimal("100.00"), "USD", mid, home=Decimal("100.00"))
    _recovered(org, Decimal("50.00"), "EUR", mid, status=Dispute.Status.CREDIT_RECEIVED, home=Decimal("54.00"))
    _recovered(org, Decimal("20.00"), "EUR", mid)                                         # no snapshot: today's rate
    _recovered(org, Decimal("999.00"), "GBP", mid)                                        # no rate at all: left out
    _recovered(org, Decimal("70.00"), "USD", datetime(2026, 8, 31, 23, tzinfo=tz))        # before the window
    _recovered(org, Decimal("30.00"), "USD", mid, status=Dispute.Status.CLOSED)           # closed: nothing recovered
    _recovered(org, Decimal("80.00"), "USD", datetime(2026, 9, 30, 23, 30, tzinfo=tz))    # last evening of the month
    assert savings.recovered(org, date(2026, 9, 1), date(2026, 9, 30)) == Decimal("256.00")
    # Datetimes: start included, end excluded.
    assert savings.recovered(org, mid, mid + timedelta(seconds=1)) == Decimal("176.00")
    assert savings.recovered(org, mid + timedelta(seconds=1), datetime(2026, 10, 1, tzinfo=tz)) == Decimal("80.00")
    assert savings.recovered(org, date(2026, 10, 1), date(2026, 10, 31)) == Decimal("0.00")
    assert savings.recovered_by_currency(org, date(2026, 9, 1), date(2026, 9, 30))["EUR"] == Decimal("70.00")


@pytest.mark.django_db
def test_disputes_list_totals_and_filters(client, user, approver, bad, issue):
    dispute = _send(_draft(bad, issue, user), approver)
    client.force_login(user)
    html = client.get(reverse("disputes:list")).content.decode()
    assert "USD 100.00" in html and dispute.reference in html and "Waiting by age" in html
    html = client.get(reverse("disputes:list") + "?status=all&vendor=nobody").content.decode()
    assert dispute.reference not in html
    html = client.get(reverse("disputes:list") + f"?status=all&vendor={dispute.vendor_key}").content.decode()
    assert dispute.reference in html
    html = client.get(reverse("disputes:list") + "?status=waiting&age=31-60").content.decode()
    assert f">{dispute.reference}<" not in html
    workflow.record_credit(dispute, approver, "100", settles=True)
    html = client.get(reverse("disputes:list") + "?status=resolved").content.decode()
    assert dispute.reference in html and "Recovered this month" in html


@pytest.mark.django_db
def test_dispute_settings_are_for_admins(client, user, admin_user):
    client.force_login(user)
    assert client.get(reverse("disputes:settings")).status_code == 403
    client.force_login(admin_user)
    r = client.post(reverse("disputes:settings"), {"reply_to": "not-an-email", "follow_up_days": "7"}, follow=True)
    assert "not a valid email address" in r.content.decode()
    client.post(reverse("disputes:settings"), {"reply_to": "ap@test.example", "follow_up_days": "10",
                                               "signature": "AP team", "copy_reply_to": "on"})
    s = DisputeSettings.objects.get()
    assert (s.reply_to, s.follow_up_days, s.signature, s.copy_reply_to) == ("ap@test.example", 10, "AP team", True)
    assert AuditEvent.objects.filter(action="dispute_settings.updated").exists()
    assert "Changed the dispute email settings" in client.get(reverse("core:audit")).content.decode()


# --------------------------------------------------------------------------- QA-065 / QA-066 / QA-067


@pytest.mark.django_db
def test_disputed_amount_cannot_exceed_the_invoice_or_be_zero_when_money_is_at_risk(bad, issue, user):
    from apps.disputes.services.workflow import _invoice_total

    draft = _draft(bad, issue, user)
    total = _invoice_total(draft)
    assert total is not None and draft.amount_disputed > 0
    common = dict(vendor_email="billing@atlas.example", contact_name="", cc="", subject=draft.subject,
                  body=draft.body, issue_ids=[issue.pk], follow_up="", remember=False)

    with pytest.raises(DisputeError, match="more than the whole invoice"):
        workflow.update_draft(draft, user, amount=str(total + 1), **common)
    with pytest.raises(DisputeError, match="can't be 0"):
        workflow.update_draft(draft, user, amount="0", **common)
    workflow.update_draft(draft, user, amount="1.00", **common)   # a smaller amount is fine
    draft.refresh_from_db()
    assert draft.amount_disputed == Decimal("1.00")


@pytest.mark.django_db
def test_a_credit_bigger_than_the_dispute_needs_confirming(approver, user, bad, issue):
    dispute = _send(_draft(bad, issue, user), approver)
    disputed = dispute.amount_disputed

    with pytest.raises(DisputeError, match="more than the amount disputed"):
        workflow.record_credit(dispute, approver, str(disputed + 10))
    dispute.refresh_from_db()
    assert dispute.amount_recovered == 0   # nothing was recorded

    workflow.record_credit(dispute, approver, str(disputed + 10), confirm_over=True)
    dispute.refresh_from_db()
    assert dispute.amount_recovered == disputed + 10


@pytest.mark.django_db
def test_a_credit_bigger_than_the_whole_invoice_is_refused_even_when_confirmed(approver, user, bad, issue):
    from apps.disputes.services.workflow import _invoice_total

    dispute = _send(_draft(bad, issue, user), approver)
    too_much = _invoice_total(dispute) + 1
    with pytest.raises(DisputeError, match="more than the whole invoice"):
        workflow.record_credit(dispute, approver, str(too_much), confirm_over=True)
    dispute.refresh_from_db()
    assert dispute.amount_recovered == 0


@pytest.mark.django_db
def test_a_credit_within_the_dispute_needs_no_confirmation(approver, user, bad, issue):
    dispute = _send(_draft(bad, issue, user), approver)
    workflow.record_credit(dispute, approver, str(dispute.amount_disputed))
    dispute.refresh_from_db()
    assert dispute.amount_recovered == dispute.amount_disputed


@pytest.mark.django_db
def test_credit_picker_offers_only_this_vendors_documents(client, approver, user, bad, issue, loaded, org):
    from apps.disputes.views import _credit_candidates

    dispute = _send(_draft(bad, issue, user), approver)
    stranger = Document.objects.create(organization=org, original_filename="other-vendor-invoice.pdf",
                                       sha256="x" * 64, doc_type="freight_invoice", status="extracted")
    ExtractedField.objects.create(document=stranger, name="vendor_name", value="Totally Different Haulage Ltd",
                                  confidence=1.0, source="human")
    shown = _credit_candidates(dispute)
    assert stranger not in shown
    assert all(d.field("vendor_name") and d.field("vendor_name").lower().startswith("atlas") for d in shown) or shown == []

    client.force_login(approver)
    page = client.get(reverse("disputes:detail", args=[dispute.pk])).content.decode()
    assert "other-vendor-invoice.pdf" not in page and 'name="confirm_over"' in page
