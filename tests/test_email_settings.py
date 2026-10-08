"""Settings > Email intake: pages, permissions, actions, scheduled checks, Gmail and the document page."""
import base64
import html

import pytest
from django.urls import reverse

from apps.core.models import AuditEvent, Organization
from apps.documents.models import Document, IngestedEmail
from apps.documents.services.ingest import ingest_bytes
from apps.mailboxes.forms import clean_sender_rules
from apps.mailboxes.models import Mailbox, MailboxMessage
from apps.mailboxes.services import gmail, imap, inbound, intake, polling
from apps.mailboxes.services.intake import IncomingAttachment, IncomingEmail

from .pdfs import make_pdf
from .test_email_imap import FakeIMAP4_SSL, FakeServer, simple_email

PDF = make_pdf("settings test invoice")


def page(response) -> str:
    return html.unescape(response.content.decode())


@pytest.fixture(autouse=True)
def _domain(settings):
    settings.INBOUND_EMAIL_DOMAIN = "in.shipmatch.test"


@pytest.fixture
def fake_imap(monkeypatch, settings):
    settings.MAILBOX_ALLOW_PRIVATE_HOSTS = True
    srv = FakeServer()
    FakeIMAP4_SSL.server = srv
    monkeypatch.setattr(imap, "IMAP4_SSL", FakeIMAP4_SSL)
    return srv


@pytest.fixture
def imap_mailbox(org):
    return Mailbox.objects.create(organization=org, kind=Mailbox.Kind.IMAP, display_name="AP inbox",
                                  host="imap.mail.example", port=993, username="ap@acme.example",
                                  password="app-password", folder="INBOX")


# ---------------------------------------------------------------- page and permissions


@pytest.mark.django_db
def test_settings_page_shows_address_instructions_and_mailboxes(client, admin_user, org, imap_mailbox):
    client.force_login(admin_user)
    r = client.get(reverse("mailboxes:index"))
    assert r.status_code == 200
    text = page(r)
    address = Mailbox.objects.get(organization=org, kind="inbound").inbound_address
    assert address in text and 'data-copy="#fwd-address"' in text
    assert "Forward from Outlook or Microsoft 365" in text and "Forward from Gmail" in text
    assert "AP inbox" in text and "Check now" in text and "Add IMAP mailbox" in text
    assert 'class="on">Email intake' in text                     # settings sub-nav
    client.get(reverse("mailboxes:index"))
    assert Mailbox.objects.filter(organization=org, kind="inbound").count() == 1


@pytest.mark.django_db
def test_settings_page_without_inbound_domain_explains_setup(client, admin_user, settings):
    settings.INBOUND_EMAIL_DOMAIN = ""
    client.force_login(admin_user)
    text = page(client.get(reverse("mailboxes:index")))
    assert "INBOUND_EMAIL_DOMAIN" in text and "Not set up" in text


@pytest.mark.django_db
@pytest.mark.parametrize("who", ["viewer", "user", "approver"])
def test_only_admins_manage_email_intake(client, request, org, imap_mailbox, who):
    client.force_login(request.getfixturevalue(who))
    pk = imap_mailbox.pk
    assert client.get(reverse("mailboxes:index")).status_code == 403
    assert client.get(reverse("mailboxes:imap_new")).status_code == 403
    assert client.get(reverse("mailboxes:edit", args=[pk])).status_code == 403
    for name in ("check", "test", "toggle", "remove", "folders"):
        assert client.post(reverse(f"mailboxes:{name}", args=[pk])).status_code == 403
    assert client.post(reverse("mailboxes:regenerate")).status_code == 403
    assert Mailbox.objects.filter(pk=pk, enabled=True).exists()


@pytest.mark.django_db
def test_mailboxes_of_other_organizations_are_hidden(client, admin_user):
    other = Organization.objects.create(name="Other Co", slug="other")
    theirs = Mailbox.objects.create(organization=other, kind=Mailbox.Kind.IMAP, host="imap.x.example", username="u")
    client.force_login(admin_user)
    assert client.get(reverse("mailboxes:edit", args=[theirs.pk])).status_code == 404
    assert client.post(reverse("mailboxes:remove", args=[theirs.pk])).status_code == 404


@pytest.mark.django_db
def test_anonymous_users_are_sent_to_sign_in(client):
    r = client.get(reverse("mailboxes:index"))
    assert r.status_code == 302 and "/account/" in r["Location"]


# ---------------------------------------------------------------- actions


@pytest.mark.django_db
def test_add_imap_mailbox_tests_the_connection(client, admin_user, org, fake_imap):
    client.force_login(admin_user)
    data = {"display_name": "", "host": "IMAP.Mail.Example:993", "port": "993", "security": "ssl",
            "username": "ap@acme.example", "password": "app-password", "folder": "", "allowed_senders": ""}
    r = client.post(reverse("mailboxes:imap_new"), data, follow=True)
    mailbox = Mailbox.objects.get(organization=org, kind="imap")
    assert mailbox.host == "imap.mail.example" and mailbox.folder == "INBOX" and mailbox.label == "ap@acme.example"
    assert mailbox.password == "app-password" and "Connected to imap.mail.example" in page(r)
    assert AuditEvent.objects.filter(organization=org, action="mailbox.connected").exists()
    assert AuditEvent.objects.filter(organization=org, action="mailbox.tested", data__ok=True).exists()

    # Wrong password: saved, but the admin lands on the settings page with the specific reason.
    data.update(password="nope", username="other@acme.example")
    r = client.post(reverse("mailboxes:imap_new"), data)
    second = Mailbox.objects.get(organization=org, kind="imap", username="other@acme.example")
    assert r["Location"] == reverse("mailboxes:edit", args=[second.pk])
    assert second.needs_reconnect and "rejected the username or password" in second.last_error


@pytest.mark.django_db
def test_imap_form_validation(client, admin_user, org):
    client.force_login(admin_user)
    r = client.post(reverse("mailboxes:imap_new"), {"host": "not a host", "port": "99999", "security": "ssl",
                                                    "username": "u", "password": "", "allowed_senders": "bad rule!"})
    text = page(r)
    assert r.status_code == 200 and not Mailbox.objects.filter(kind="imap").exists()
    assert "Enter the server name only" in text and "Enter the password" in text
    assert "aren't email addresses or domains: bad, rule!" in text


@pytest.mark.django_db
def test_edit_keeps_the_saved_password_when_left_empty(client, admin_user, org, imap_mailbox, fake_imap):
    client.force_login(admin_user)
    r = client.post(reverse("mailboxes:edit", args=[imap_mailbox.pk]), {
        "display_name": "AP inbox", "host": "imap.mail.example", "port": "993", "security": "ssl",
        "username": "ap@acme.example", "password": "", "folder": "Invoices", "mark_seen": "on",
        "allowed_senders": "Billing@Harborlink.example, ocean.example"})
    assert r.status_code == 302
    imap_mailbox.refresh_from_db()
    assert imap_mailbox.password == "app-password" and imap_mailbox.folder == "Invoices" and imap_mailbox.mark_seen
    assert imap_mailbox.sender_rules() == ["billing@harborlink.example", "ocean.example"]


@pytest.mark.django_db
def test_check_now_imports_and_reports(client, admin_user, org, imap_mailbox, fake_imap):
    fake_imap.add("INBOX", 3, simple_email(3, PDF))
    client.force_login(admin_user)
    r = client.post(reverse("mailboxes:check", args=[imap_mailbox.pk]), follow=True)
    assert "1 new email, 1 new document added" in page(r)
    assert Document.objects.filter(organization=org, source="email").count() == 1
    imap_mailbox.refresh_from_db()
    assert imap_mailbox.last_checked_at and imap_mailbox.last_success_at and not imap_mailbox.last_error
    text = page(client.get(reverse("mailboxes:index")))
    assert "Invoice 3" in text and "1 document added" in text and "invoice-3.pdf" in text


@pytest.mark.django_db
def test_pause_resume_and_remove(client, admin_user, org, imap_mailbox):
    client.force_login(admin_user)
    client.post(reverse("mailboxes:toggle", args=[imap_mailbox.pk]))
    imap_mailbox.refresh_from_db()
    assert not imap_mailbox.enabled and imap_mailbox.status_label == "Paused"
    r = client.post(reverse("mailboxes:check", args=[imap_mailbox.pk]), follow=True)
    assert "is paused" in page(r)
    client.post(reverse("mailboxes:toggle", args=[imap_mailbox.pk]))
    imap_mailbox.refresh_from_db()
    assert imap_mailbox.enabled
    client.post(reverse("mailboxes:remove", args=[imap_mailbox.pk]))
    assert not Mailbox.objects.filter(pk=imap_mailbox.pk).exists()
    assert list(AuditEvent.objects.filter(organization=org, action__startswith="mailbox.")
                .values_list("action", flat=True).order_by("id")) == \
        ["mailbox.disabled", "mailbox.enabled", "mailbox.removed"]


@pytest.mark.django_db
def test_forwarding_address_can_be_renewed_but_not_removed(client, admin_user, org):
    client.force_login(admin_user)
    client.get(reverse("mailboxes:index"))
    forwarding = Mailbox.objects.get(organization=org, kind="inbound")
    old = forwarding.inbound_address
    r = client.post(reverse("mailboxes:regenerate"), follow=True)
    forwarding.refresh_from_db()
    assert forwarding.inbound_address != old and forwarding.inbound_address in page(r)
    assert AuditEvent.objects.filter(organization=org, action="inbound_address.regenerated", data__old=old).exists()
    r = client.post(reverse("mailboxes:remove", args=[forwarding.pk]), follow=True)
    assert "can't be removed" in page(r) and Mailbox.objects.filter(pk=forwarding.pk).exists()
    client.post(reverse("mailboxes:edit", args=[forwarding.pk]), {"allowed_senders": "acme.example"})
    forwarding.refresh_from_db()
    assert forwarding.sender_rules() == ["acme.example"]


# ---------------------------------------------------------------- scheduled checks


@pytest.mark.django_db
def test_one_failing_mailbox_never_stops_the_others(org, monkeypatch):
    from apps.mailboxes.tasks import poll_all_mailboxes

    broken = Mailbox.objects.create(organization=org, kind=Mailbox.Kind.IMAP, display_name="Broken", host="a.example")
    good = Mailbox.objects.create(organization=org, kind=Mailbox.Kind.IMAP, display_name="Good", host="b.example")
    Mailbox.objects.create(organization=org, kind=Mailbox.Kind.IMAP, display_name="Paused", host="c.example",
                           enabled=False)
    checked = []

    def provider(kind):
        def run(mailbox, process="async"):
            checked.append(mailbox.display_name)
            if mailbox.display_name == "Broken":
                raise RuntimeError("library bug")
            return {"emails": 0, "documents": 0}
        return run

    monkeypatch.setattr(polling, "_provider", provider)
    result = poll_all_mailboxes()
    assert sorted(checked) == ["Broken", "Good"] and result["queued"] == 2
    broken.refresh_from_db()
    good.refresh_from_db()
    assert "library bug" in broken.last_error and broken.last_checked_at
    assert good.last_success_at and not good.last_error


@pytest.mark.django_db
def test_concurrent_checks_of_one_mailbox_are_skipped(org, imap_mailbox):
    from django.core.cache import cache

    cache.add(f"mailbox-poll:{imap_mailbox.pk}", 1, 60)
    assert polling.poll_mailbox(imap_mailbox)["status"] == "busy"


@pytest.mark.django_db
def test_old_gmail_task_names_still_work(org, monkeypatch):
    from apps.documents import tasks as old_tasks

    monkeypatch.setattr(polling, "_provider", lambda kind: lambda mb, process="async": {"emails": 0})
    Mailbox.objects.create(organization=org, kind=Mailbox.Kind.IMAP, host="b.example")
    assert old_tasks.poll_all_mailboxes()["queued"] == 1


# ---------------------------------------------------------------- Gmail


class FakeGmail:
    """The slice of the Gmail API client used by the wrapper (request objects with .execute())."""

    def __init__(self, messages):
        self.messages_by_id = messages
        self.queries = []

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return self

    def labels(self):
        return self

    def getProfile(self, userId):
        return _Req({"emailAddress": "ap@gmail.example"})

    def list(self, userId, q=None, maxResults=None, pageToken=None):
        if q is None:
            return _Req({"labels": [{"name": "AP-Inbox"}]})
        self.queries.append(q)
        ids = [{"id": i} for i in reversed(list(self.messages_by_id))]   # newest first, like Gmail
        return _Req({"messages": ids})

    def get(self, userId, id=None, messageId=None, format=None):
        if messageId:
            return _Req({"data": base64.urlsafe_b64encode(PDF).decode()})
        return _Req(self.messages_by_id[id])


class _Req:
    def __init__(self, value, owner=None):
        self.value = value

    def execute(self):
        return self.value


def gmail_message(gid, message_id, internal_ms):
    return {"id": gid, "internalDate": str(internal_ms), "payload": {
        "headers": [{"name": "From", "value": "Carrier <billing@ocean.example>"}, {"name": "Subject", "value": "FI"},
                    {"name": "Message-ID", "value": f"<{message_id}>"}],
        "parts": [{"mimeType": "text/plain", "filename": "", "body": {"data": "aGk="}},
                  {"mimeType": "application/pdf", "filename": "FI-77.pdf", "headers": [],
                   "body": {"attachmentId": "att-1", "size": len(PDF)}}]}}


@pytest.mark.django_db
def test_gmail_appears_as_a_mailbox_and_uses_the_shared_intake(org, tmp_path, monkeypatch, settings):
    token = tmp_path / "gmail_token_test.json"
    monkeypatch.setattr(gmail.gmail_api, "token_path", lambda o: token)
    assert gmail.sync(org) is None
    token.write_text("{}")
    mailbox = gmail.sync(org)
    assert mailbox.kind == "gmail" and mailbox.folder == settings.GMAIL_LABEL
    assert gmail.sync(org).pk == mailbox.pk

    # An email the old poller stored under its Gmail ID is not imported again.
    IngestedEmail.objects.create(organization=org, message_id="g-old")
    service = FakeGmail({"g-old": gmail_message("g-old", "old@x", 1_790_000_000_000),
                         "g-new": gmail_message("g-new", "new@x", 1_790_000_500_000)})
    stats = gmail.poll(mailbox, service=service)
    assert stats["emails"] == 1 and stats["already"] == 1
    assert service.queries[0].startswith("label:AP-Inbox has:attachment newer_than:30d")
    msg = MailboxMessage.objects.get(email__message_id="new@x")
    assert msg.outcome == "documents" and msg.attachments.get().filename == "FI-77.pdf"
    mailbox.refresh_from_db()
    assert mailbox.cursor == "1790000500"
    gmail.poll(mailbox, service=service)
    assert "after:1790000500" in service.queries[-1]
    assert "Connected to ap@gmail.example" in gmail.test_connection(mailbox, service=service)

    token.unlink()
    mailbox = gmail.sync(org)
    assert mailbox.needs_reconnect and "gmail_auth --org test" in mailbox.last_error


# ---------------------------------------------------------------- intake rules and the document page


def test_sender_rules():
    mb = Mailbox(allowed_senders="billing@harborlink.example\n@ocean.example\n*.carrier.example")
    assert mb.sender_allowed("Billing@Harborlink.example")
    assert not mb.sender_allowed("other@harborlink.example")
    assert mb.sender_allowed("x@ocean.example") and mb.sender_allowed("x@eu.ocean.example")
    assert not mb.sender_allowed("x@notocean.example") and not mb.sender_allowed("")
    assert mb.sender_allowed("a@b.carrier.example")
    assert Mailbox(allowed_senders="").sender_allowed("anyone@anywhere.example")
    assert clean_sender_rules("A@B.example, b.example;\nb.example") == "a@b.example\nb.example"


def test_inline_and_tiny_images_are_skipped_but_real_scans_are_offered():
    logo = IncomingAttachment("logo.png", "image/png", b"x" * 40_000, inline=True)
    pixel = IncomingAttachment("p.gif", "image/gif", b"x" * 300)
    scan = IncomingAttachment("scan.jpg", "image/jpeg", b"x" * 400_000, inline=True)
    attached_photo = IncomingAttachment("photo.jpg", "image/jpeg", b"x" * 40_000)
    assert intake.skip_reason(logo).startswith("Picture inside the email")
    assert intake.skip_reason(pixel).startswith("Tiny image")
    assert intake.skip_reason(scan) == "" and intake.skip_reason(attached_photo) == ""
    assert not intake.has_documents(IncomingEmail("x", attachments=[logo, pixel]))
    assert intake.has_documents(IncomingEmail("x", attachments=[logo, attached_photo]))


@pytest.mark.django_db
def test_document_page_shows_the_email_it_came_from(client, user, org, dataset):
    forwarding = inbound.ensure_inbound(org)
    pdf = (dataset / "pdf" / "S01_1_commercial_invoice.pdf").read_bytes()
    incoming = IncomingEmail(message_id="doc-page@x", subject="Commercial invoice for PO 4471",
                             sender="Lumen Trading <ar@lumen.example>", attachments=[
                                 IncomingAttachment("CI.pdf", "application/pdf", pdf),
                                 IncomingAttachment("terms.p7s", "application/pkcs7-signature", b"sig" * 100)])
    result = intake.receive(forwarding, incoming, process="none")
    doc = result.new_documents[0]
    client.force_login(user)
    text = page(client.get(reverse("review:document", args=[doc.pk])))
    assert "Received by email" in text and "Commercial invoice for PO 4471" in text
    assert "ar@lumen.example" in text and "Forwarding address" in text
    assert "terms.p7s" in text and "This document" in text

    # Once matched, the document is shown on the shipment page with a one-line email note.
    from apps.documents.services.pipeline import process_document

    ingest_bytes(org, "BL.pdf", (dataset / "pdf" / "S01_2_bill_of_lading.pdf").read_bytes(), process="sync")
    process_document(doc.pk)
    doc = Document.objects.get(pk=doc.pk)
    text = page(client.get(reverse("review:shipment", args=[doc.match.shipment_id])))
    assert "Emailed by Lumen Trading <ar@lumen.example>" in text and "received through Forwarding address" in text


# ---------------------------------------------------------------- QA-041: a wrong server leaves nothing behind


def _imap_form(host, **over):
    data = {"display_name": "", "host": host, "port": "993", "security": "ssl", "username": "ap@acme.example",
            "password": "app-password", "folder": "", "allowed_senders": ""}
    data.update(over)
    return data


@pytest.mark.django_db
@pytest.mark.parametrize("host, resolves_to, words", [
    ("mail.internal.example", "10.0.0.5", "private network"),
    ("loopback.example", "127.0.0.1", "private network"),
    ("metadata.example", "169.254.169.254", "private network"),
    ("carrier-grade.example", "100.64.0.1", "private network"),
])
def test_a_private_server_is_refused_and_no_mailbox_is_saved(client, admin_user, org, settings, monkeypatch,
                                                              host, resolves_to, words):
    import socket

    settings.MAILBOX_ALLOW_PRIVATE_HOSTS = False
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (resolves_to, 0))])
    client.force_login(admin_user)

    r = client.post(reverse("mailboxes:imap_new"), _imap_form(host))

    assert r.status_code == 200 and words in page(r)
    assert not Mailbox.objects.filter(organization=org).exists()
    assert not AuditEvent.objects.filter(organization=org, action="mailbox.connected").exists()


@pytest.mark.django_db
def test_a_server_name_that_does_not_exist_is_refused_and_nothing_is_saved(client, admin_user, org, settings,
                                                                           monkeypatch):
    import socket

    settings.MAILBOX_ALLOW_PRIVATE_HOSTS = False

    def nxdomain(*args, **kwargs):
        raise socket.gaierror("not known")

    monkeypatch.setattr(socket, "getaddrinfo", nxdomain)
    client.force_login(admin_user)

    r = client.post(reverse("mailboxes:imap_new"), _imap_form("imap://evil.example"))

    assert "Couldn't find the server" in page(r) and not Mailbox.objects.filter(organization=org).exists()


@pytest.mark.django_db
def test_a_wrong_password_still_keeps_the_mailbox_for_correcting(client, admin_user, org, fake_imap):
    """Unchanged on purpose: the server is fine, so the mailbox is saved and the admin fixes the password."""
    client.force_login(admin_user)
    client.post(reverse("mailboxes:imap_new"), _imap_form("imap.mail.example", password="nope"))
    assert Mailbox.objects.filter(organization=org, username="ap@acme.example").exists()


@pytest.mark.django_db
def test_changing_an_existing_mailbox_to_a_private_server_is_refused(client, admin_user, imap_mailbox, settings,
                                                                     monkeypatch):
    import socket

    settings.MAILBOX_ALLOW_PRIVATE_HOSTS = False
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 0))])
    client.force_login(admin_user)
    before = imap_mailbox.host

    r = client.post(reverse("mailboxes:edit", args=[imap_mailbox.pk]), _imap_form("intranet.example", password=""))

    assert "private network" in page(r)
    imap_mailbox.refresh_from_db()
    assert imap_mailbox.host == before
