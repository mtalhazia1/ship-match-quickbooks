"""Forwarding addresses: Postmark and Mailgun webhooks, routing, signatures, replays, duplicates and limits."""
import base64
import hashlib
import hmac
import json
import time

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.core.models import AuditEvent, Organization
from apps.documents.models import Document, IngestedEmail
from apps.mailboxes.models import EmailAttachment, Mailbox, MailboxMessage
from apps.mailboxes.services import inbound

from .pdfs import make_pdf

DOMAIN = "in.shipmatch.test"
PDF = make_pdf("freight invoice FI-1001")
PDF2 = make_pdf("bill of lading HLB-777")
LOGO = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20_000   # a 20 KB signature logo
KEY = "mg-signing-key-test"


@pytest.fixture(autouse=True)
def _inbound_settings(settings):
    settings.INBOUND_EMAIL_DOMAIN = DOMAIN
    settings.POSTMARK_INBOUND_USER = "postmark"
    settings.POSTMARK_INBOUND_PASSWORD = "s3cret-pass"
    settings.MAILGUN_SIGNING_KEY = KEY
    settings.INBOUND_EMAIL_RATE_PER_MINUTE = 60


@pytest.fixture
def address(org):
    return inbound.ensure_inbound(org).inbound_address


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def postmark_payload(to, message_id="<abc-1@mail.harborlink.example>", sender="billing@harborlink.example",
                     attachments=None, subject="Invoice FI-1001"):
    return {
        "From": sender, "FromFull": {"Email": sender, "Name": "Harborlink Billing"},
        "To": to, "ToFull": [{"Email": to, "Name": ""}], "OriginalRecipient": to,
        "Subject": subject, "MessageID": "pm-0001", "Date": "Fri, 2 Oct 2026 09:15:00 +0000",
        "TextBody": "Please find the invoice attached.",
        "Headers": [{"Name": "Message-ID", "Value": message_id}],
        "Attachments": attachments if attachments is not None else [
            {"Name": "FI-1001.pdf", "Content": b64(PDF), "ContentType": "application/pdf", "ContentLength": len(PDF)},
            {"Name": "image001.png", "Content": b64(LOGO), "ContentType": "image/png", "ContentID": "image001.png@01DB"},
            {"Name": "smime.p7s", "Content": b64(b"signature"), "ContentType": "application/pkcs7-signature"},
        ],
    }


def post_postmark(client, payload, user="postmark", password="s3cret-pass"):
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    return client.post(reverse("inbound:postmark"), data=json.dumps(payload), content_type="application/json",
                        HTTP_AUTHORIZATION=auth)


# ---------------------------------------------------------------- addresses


@pytest.mark.django_db
def test_forwarding_address_is_unique_and_unguessable(org):
    other = Organization.objects.create(name="Other", slug="test-two")
    a, b = inbound.ensure_inbound(org), inbound.ensure_inbound(other)
    assert inbound.ensure_inbound(org).pk == a.pk          # one per organization
    assert a.inbound_address.startswith("test-") and a.inbound_address.endswith("@" + DOMAIN)
    token = a.inbound_local.rsplit("-", 1)[1]
    assert len(token) == 16 and token.isalnum()            # 80 random bits
    assert a.inbound_local != b.inbound_local
    old = a.inbound_local
    inbound.regenerate(a)
    assert a.inbound_local != old and a.inbound_local.startswith("test-")


@pytest.mark.django_db
def test_routing_ignores_case_plus_tags_and_other_domains(org, address):
    local = address.split("@")[0]
    mailbox, recipient = inbound.route(["someone@else.example", f"{local.upper()}+invoices@{DOMAIN.upper()}"])
    assert mailbox.organization == org and recipient.startswith(local)
    assert inbound.route([f"{local}@evil.example"])[0] is None


# ---------------------------------------------------------------- Postmark


@pytest.mark.django_db
def test_postmark_imports_attachments_and_records_each_outcome(org, address):
    client = Client(enforce_csrf_checks=True)   # webhooks work without a CSRF token
    r = post_postmark(client, postmark_payload(address))
    assert r.status_code == 200 and r.json() == {"status": "accepted"}

    email = IngestedEmail.objects.get(organization=org)
    assert email.message_id == "abc-1@mail.harborlink.example"
    assert email.subject == "Invoice FI-1001" and "billing@harborlink.example" in email.sender
    doc = Document.objects.get(organization=org)
    assert doc.source == Document.Source.EMAIL and doc.email == email and doc.original_filename == "FI-1001.pdf"

    msg = MailboxMessage.objects.get(email=email)
    assert msg.outcome == MailboxMessage.Outcome.DOCUMENTS and msg.documents_created == 1
    assert msg.mailbox.kind == Mailbox.Kind.INBOUND and msg.recipient == address
    outcomes = {a.filename: (a.outcome, a.reason) for a in EmailAttachment.objects.filter(message=msg)}
    assert outcomes["FI-1001.pdf"][0] == "imported"
    assert outcomes["image001.png"] == ("skipped", "Picture inside the email text (signature or logo)")
    assert outcomes["smime.p7s"][0] == "skipped" and outcomes["smime.p7s"][1]   # ingest decided and said why
    mailbox = msg.mailbox
    mailbox.refresh_from_db()
    assert (mailbox.emails_received, mailbox.documents_received, mailbox.attachments_skipped) == (1, 1, 2)
    assert AuditEvent.objects.filter(organization=org, action="email.received").exists()


@pytest.mark.django_db
def test_postmark_duplicate_message_id_is_recorded_once(client, org, address):
    assert post_postmark(client, postmark_payload(address)).json()["status"] == "accepted"
    again = postmark_payload(address, attachments=[
        {"Name": "other.pdf", "Content": b64(PDF2), "ContentType": "application/pdf"}])
    r = post_postmark(client, again)
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert IngestedEmail.objects.filter(organization=org).count() == 1
    assert Document.objects.filter(organization=org).count() == 1   # the retry didn't import anything new


@pytest.mark.django_db
def test_same_file_in_a_new_email_links_to_the_existing_document(client, org, address):
    post_postmark(client, postmark_payload(address))
    post_postmark(client, postmark_payload(address, message_id="<forwarded-again@x>"))
    msg = MailboxMessage.objects.get(email__message_id="forwarded-again@x")
    assert msg.outcome == MailboxMessage.Outcome.DUPLICATES
    pdf = msg.attachments.get(filename="FI-1001.pdf")
    assert pdf.outcome == "duplicate" and pdf.document == Document.objects.get(organization=org)


@pytest.mark.django_db
def test_postmark_rejects_bad_or_missing_credentials(client, org, address, settings):
    assert post_postmark(client, postmark_payload(address), password="wrong").status_code == 401
    r = client.post(reverse("inbound:postmark"), data=json.dumps(postmark_payload(address)),
                    content_type="application/json")
    assert r.status_code == 401 and "Basic" in r["WWW-Authenticate"]
    settings.POSTMARK_INBOUND_PASSWORD = ""   # not configured: fail closed
    assert post_postmark(client, postmark_payload(address), password="").status_code == 401
    assert not IngestedEmail.objects.exists()


@pytest.mark.django_db
def test_unknown_and_paused_addresses_get_the_same_rejection(client, org, address):
    unknown = post_postmark(client, postmark_payload(f"test-aaaaaaaaaaaaaaaa@{DOMAIN}"))
    Mailbox.objects.filter(kind="inbound").update(enabled=False)
    paused = post_postmark(client, postmark_payload(address))
    assert unknown.status_code == paused.status_code == 403        # Postmark stops retrying on 403
    assert unknown.content == paused.content                        # nothing reveals which addresses exist
    assert b"test" not in unknown.content.lower()
    assert not IngestedEmail.objects.exists()


@pytest.mark.django_db
def test_regenerated_address_stops_the_old_one(client, org, address):
    inbound.regenerate(Mailbox.objects.get(organization=org, kind="inbound"))
    assert post_postmark(client, postmark_payload(address)).status_code == 403


@pytest.mark.django_db
def test_postmark_rejects_bad_json_oversized_and_get(client, org, address, settings):
    auth = "Basic " + base64.b64encode(b"postmark:s3cret-pass").decode()
    r = client.post(reverse("inbound:postmark"), data="{not json", content_type="application/json",
                    HTTP_AUTHORIZATION=auth)
    assert r.status_code == 400
    settings.INBOUND_EMAIL_MAX_MB = 1
    big = postmark_payload(address, attachments=[
        {"Name": "huge.pdf", "Content": b64(b"%PDF" + b"0" * 1_200_000), "ContentType": "application/pdf"}])
    assert post_postmark(client, big).status_code == 403
    assert client.get(reverse("inbound:postmark")).status_code == 405


@pytest.mark.django_db
def test_allowed_senders_block_other_senders(client, org, address):
    Mailbox.objects.filter(organization=org, kind="inbound").update(allowed_senders="harborlink.example\nap@acme.test")
    r = post_postmark(client, postmark_payload(address, sender="random@spam.example"))
    assert r.status_code == 200   # accepted so the provider doesn't retry, but nothing imported
    msg = MailboxMessage.objects.get(organization=org)
    assert msg.outcome == MailboxMessage.Outcome.BLOCKED and "random@spam.example" in msg.note
    assert not Document.objects.exists()
    r = post_postmark(client, postmark_payload(address, message_id="<ok@x>", sender="billing@eu.harborlink.example"))
    assert Document.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
def test_gmail_forwarding_confirmation_code_is_shown(client, org, address):
    payload = postmark_payload(address, sender="forwarding-noreply@google.com", attachments=[],
                               subject="(#518206412) Gmail Forwarding Confirmation - Receive Mail from ap@acme.test")
    assert post_postmark(client, payload).status_code == 200
    msg = MailboxMessage.objects.get(organization=org)
    assert msg.outcome == MailboxMessage.Outcome.NOTHING
    assert "518206412" in msg.note and "ap@acme.test" in msg.note


@pytest.mark.django_db
def test_inbound_rate_limit_asks_the_provider_to_retry(client, org, address, settings, monkeypatch):
    settings.INBOUND_EMAIL_RATE_PER_MINUTE = 1
    monkeypatch.setattr("apps.mailboxes.webhooks.time.time", lambda: 1_800_000_030.0)   # both posts in one minute
    assert post_postmark(client, postmark_payload(address)).status_code == 200
    r = post_postmark(client, postmark_payload(address, message_id="<second@x>"))
    assert r.status_code == 429 and r["Retry-After"] == "60"


# ---------------------------------------------------------------- Mailgun


def mailgun_fields(to, token="tok-" + "a" * 46, timestamp=None, key=KEY, message_id="<mg-1@harborlink.example>",
                   sender="Harborlink Billing <billing@harborlink.example>"):
    ts = str(int(timestamp if timestamp is not None else time.time()))
    sig = hmac.new(key.encode(), f"{ts}{token}".encode(), hashlib.sha256).hexdigest()
    return {
        "recipient": to, "sender": "billing@harborlink.example", "from": sender, "subject": "B/L HLB-777",
        "body-plain": "Bill of lading attached", "timestamp": ts, "token": token, "signature": sig,
        "message-headers": json.dumps([["Message-Id", message_id], ["Date", "Fri, 02 Oct 2026 10:00:00 +0000"]]),
        "content-id-map": json.dumps({"<logo@x>": "attachment-2"}), "attachment-count": "2",
        "attachment-1": SimpleUploadedFile("HLB-777.pdf", PDF2, content_type="application/pdf"),
        "attachment-2": SimpleUploadedFile("logo.png", LOGO, content_type="image/png"),
    }


@pytest.mark.django_db
def test_mailgun_route_imports_attachments(org, address):
    client = Client(enforce_csrf_checks=True)
    r = client.post(reverse("inbound:mailgun"), data=mailgun_fields(address))
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    msg = MailboxMessage.objects.get(organization=org)
    assert msg.email.message_id == "mg-1@harborlink.example"
    assert {a.filename: a.outcome for a in msg.attachments.all()} == {"HLB-777.pdf": "imported", "logo.png": "skipped"}
    assert Document.objects.get(organization=org).original_filename == "HLB-777.pdf"


@pytest.mark.django_db
def test_mailgun_rejects_bad_signature_stale_and_replayed_requests(client, org, address):
    url = reverse("inbound:mailgun")
    assert client.post(url, data=mailgun_fields(address, key="wrong-key")).status_code == 403
    stale = mailgun_fields(address, token="t-stale", timestamp=time.time() - 3600)
    assert client.post(url, data=stale).status_code == 406
    assert client.post(url, data=mailgun_fields(address, token="t-once")).status_code == 200
    replay = mailgun_fields(address, token="t-once", message_id="<different@x>")
    assert client.post(url, data=replay).status_code == 406       # same token: replay, Mailgun won't retry
    assert IngestedEmail.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
def test_mailgun_unknown_recipient_and_duplicate(client, org, address):
    url = reverse("inbound:mailgun")
    r = client.post(url, data=mailgun_fields(f"nobody-0000000000000000@{DOMAIN}", token="t-1"))
    assert r.status_code == 406 and r.content == b"This address does not accept email."
    assert client.post(url, data=mailgun_fields(address, token="t-2")).json()["status"] == "accepted"
    assert client.post(url, data=mailgun_fields(address, token="t-3")).json()["status"] == "duplicate"
    assert IngestedEmail.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
def test_mailgun_error_releases_the_token_so_the_retry_works(org, address, monkeypatch):
    from apps.mailboxes.services import intake

    real = intake.receive
    monkeypatch.setattr(intake, "receive", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("storage down")))
    client = Client(raise_request_exception=False)
    fields = mailgun_fields(address, token="t-retry")
    assert client.post(reverse("inbound:mailgun"), data=fields).status_code == 500   # Mailgun retries 5xx
    monkeypatch.setattr(intake, "receive", real)
    assert client.post(reverse("inbound:mailgun"), data=mailgun_fields(address, token="t-retry")).status_code == 200
    assert Document.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
def test_mailgun_without_signing_key_rejects_everything(client, org, address, settings):
    settings.MAILGUN_SIGNING_KEY = ""
    assert client.post(reverse("inbound:mailgun"), data=mailgun_fields(address)).status_code == 403
