"""IMAP mailboxes against a fake IMAP4_SSL server, and MIME parsing of real-world email shapes."""
import imaplib
import socket
from email.message import EmailMessage
from email.mime.application import MIMEApplication
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest
from django.urls import reverse

from apps.core.models import AuditEvent
from apps.documents.models import Document
from apps.mailboxes.models import Mailbox, MailboxMessage
from apps.mailboxes.services import imap
from apps.mailboxes.services.mime import imap_quote, imap_utf7_decode, imap_utf7_encode, parse_message
from apps.mailboxes.services.polling import poll_mailbox

from .pdfs import make_pdf

PDF_1 = make_pdf("invoice one")
PDF_2 = make_pdf("invoice two")
PDF_3 = make_pdf("bill of lading three")
PDF_FWD = make_pdf("forwarded freight invoice")
PDF_DE = make_pdf("Rechnung Fracht")


def simple_email(n: int, pdf: bytes | None, sender="billing@harborlink.example") -> bytes:
    msg = EmailMessage()
    msg["From"] = f"Harborlink <{sender}>"
    msg["To"] = "ap@acme.example"
    msg["Subject"] = f"Invoice {n}"
    msg["Message-ID"] = f"<inv-{n}@harborlink.example>"
    msg["Date"] = "Thu, 01 Oct 2026 10:00:00 +0000"
    msg.set_content("Invoice attached.")
    if pdf:
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=f"invoice-{n}.pdf")
    return msg.as_bytes()


def nested_email() -> bytes:
    """mixed > (alternative > text, related > html + inline logo), RFC 2231 PDF, QP CSV, forwarded email with PDF."""
    root = MIMEMultipart("mixed")
    root["From"] = "=?utf-8?q?M=C3=BCller_Spedition?= <rechnung@mueller.example>"
    root["Subject"] = "=?utf-8?b?UmVjaG51bmcgTcOkcnogLyBmcmVpZ2h0IGludm9pY2U=?="
    root["Message-ID"] = "<nested-1@mueller.example>"
    root["Date"] = "Fri, 02 Oct 2026 11:00:00 +0200"
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("Rechnung anbei.", "plain", "utf-8"))
    related = MIMEMultipart("related")
    related.attach(MIMEText('<p>Rechnung anbei <img src="cid:logo1"></p>', "html", "utf-8"))
    logo = MIMEImage(b"\x89PNG\r\n\x1a\n" + b"\x01" * 12_000, "png")
    logo.add_header("Content-ID", "<logo1>")
    logo.add_header("Content-Disposition", "inline", filename="logo.png")
    related.attach(logo)
    alt.attach(related)
    root.attach(alt)
    pdf = MIMEApplication(PDF_DE, "pdf")   # base64
    pdf.add_header("Content-Disposition", "attachment", filename=("utf-8", "", "Rechnung März 2026.pdf"))
    root.attach(pdf)
    csv = MIMEBase("text", "csv")
    csv.set_payload("Position;Betrag\nFracht;1200,00 €\n".encode())
    from email import encoders
    encoders.encode_quopri(csv)
    csv.add_header("Content-Disposition", "attachment", filename="positionen.csv")
    root.attach(csv)
    inner = MIMEMultipart("mixed")
    inner["Subject"] = "Original freight invoice"
    inner["From"] = "carrier@ocean.example"
    inner.attach(MIMEText("see attached", "plain"))
    fwd_pdf = MIMEApplication(PDF_FWD, "pdf")
    fwd_pdf.add_header("Content-Disposition", "attachment", filename="FI-900.pdf")
    inner.attach(fwd_pdf)
    wrapper = MIMEBase("message", "rfc822")
    wrapper.set_payload([inner])
    wrapper.add_header("Content-Disposition", "attachment", filename="Original.eml")
    root.attach(wrapper)
    raw = root.as_bytes()
    # Long encoded filenames are split into RFC 2231 continuations by real clients; make sure that shape parses.
    return raw.replace(b"filename*=utf-8''Rechnung%20M%C3%A4rz%202026.pdf",
                       b"filename*0*=utf-8''Rechnung%20M%C3%A4rz;\n filename*1*=%202026.pdf")


class FakeServer:
    def __init__(self):
        self.password = "app-password"
        self.validity = "1001"
        self.folders = {"INBOX": {}, "Invoices": {}, "Archive": {}}
        self.seen, self.commands, self.selected_readonly = set(), [], []

    def add(self, folder, uid, raw):
        self.folders[folder][uid] = raw


class FakeIMAP4_SSL:
    server: FakeServer = None

    def __init__(self, host, port, ssl_context=None, timeout=None):
        if host == "nowhere.example":
            raise socket.gaierror(-2, "Name or service not known")
        if host == "closed.example":
            raise ConnectionRefusedError(111, "Connection refused")
        self.host, self.port = host, port
        self.untagged = {}
        self.folder = None

    def login(self, user, password):
        if password != self.server.password:
            raise imaplib.IMAP4.error("b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'")
        return "OK", [b"LOGIN completed"]

    def select(self, mailbox, readonly=False):
        name = mailbox.strip('"')
        if name not in self.server.folders:
            return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
        self.folder = name
        self.server.selected_readonly.append(readonly)
        self.untagged = {"UIDVALIDITY": [self.server.validity.encode()]}
        return "OK", [str(len(self.server.folders[name])).encode()]

    def response(self, code):
        return code, self.untagged.pop(code, [None])

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Archive"',
                      b'(\\Noselect \\HasChildren) "/" "[Gmail]"', b'(\\HasNoChildren) "/" "&AMQ-rger"']

    def uid(self, command, *args):
        self.server.commands.append((command, *args))
        messages = self.server.folders[self.folder]
        if command == "SEARCH":
            if args[1] == "SINCE":
                found = sorted(messages)
            else:   # "UID n:*": like real servers, returns the newest message even when n is past the end
                start = int(args[2].split(":")[0])
                found = [u for u in sorted(messages) if u >= start] or ([max(messages)] if messages else [])
            return "OK", [" ".join(str(u) for u in found).encode()]
        if command == "FETCH":
            uid = int(args[0])
            if uid not in messages:
                return "OK", [None]
            raw = messages[uid]
            return "OK", [(f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        if command == "STORE":
            self.server.seen.add(int(args[0]))
            return "OK", [b""]
        raise AssertionError(command)

    def logout(self):
        return "BYE", [b""]


@pytest.fixture
def server(monkeypatch, settings):
    settings.MAILBOX_ALLOW_PRIVATE_HOSTS = True   # no DNS lookups in tests
    srv = FakeServer()
    FakeIMAP4_SSL.server = srv
    monkeypatch.setattr(imap, "IMAP4_SSL", FakeIMAP4_SSL)
    return srv


@pytest.fixture
def mailbox(org):
    return Mailbox.objects.create(organization=org, kind=Mailbox.Kind.IMAP, display_name="AP inbox",
                                  host="imap.mail.example", port=993, username="ap@acme.example",
                                  password="app-password", folder="INBOX", folder_name="INBOX")


# ---------------------------------------------------------------- MIME


def test_parse_nested_multipart_rfc2231_and_forwarded_email():
    parsed = parse_message(nested_email())
    assert parsed.message_id == "nested-1@mueller.example"
    assert parsed.subject == "Rechnung März / freight invoice"
    assert "Müller Spedition" in parsed.sender and parsed.sender_address == "rechnung@mueller.example"
    assert parsed.received_at.isoformat() == "2026-10-02T11:00:00+02:00"
    by_name = {a.filename: a for a in parsed.attachments}
    assert set(by_name) == {"logo.png", "Rechnung März 2026.pdf", "positionen.csv", "FI-900.pdf"}
    assert by_name["Rechnung März 2026.pdf"].content == PDF_DE                 # base64 decoded
    assert "1200,00 €" in by_name["positionen.csv"].content.decode()          # quoted-printable decoded
    assert by_name["FI-900.pdf"].content == PDF_FWD                           # inside the forwarded email
    assert by_name["logo.png"].inline and not by_name["FI-900.pdf"].inline
    assert parsed.text.startswith("Rechnung anbei")


def test_missing_message_id_gets_a_stable_key():
    raw = simple_email(1, PDF_1).replace(b"Message-ID: <inv-1@harborlink.example>\n", b"")
    a, b = parse_message(raw, "imap", "host", 7), parse_message(raw, "imap", "host", 7)
    assert a.message_id == b.message_id and a.message_id.startswith("sha256:")


def test_imap_folder_names_are_encoded():
    assert imap_utf7_encode("Rechnungen/Eingänge") == "Rechnungen/Eing&AOQ-nge"
    assert imap_utf7_decode("Rechnungen/Eing&AOQ-nge") == "Rechnungen/Eingänge"
    assert imap_utf7_encode("R&D") == "R&-D" and imap_utf7_decode("R&-D") == "R&D"
    assert imap_quote("INBOX") == "INBOX" and imap_quote("AP Invoices") == '"AP Invoices"'


# ---------------------------------------------------------------- polling


@pytest.mark.django_db
def test_poll_imports_by_uid_and_only_moves_forward(org, mailbox, server):
    server.add("INBOX", 4, simple_email(4, PDF_1))
    server.add("INBOX", 5, simple_email(5, None))            # no attachment: not recorded
    server.add("INBOX", 6, nested_email())
    stats = imap.poll(mailbox)
    assert stats["emails"] == 2 and stats["without_attachments"] == 1
    names = set(Document.objects.filter(organization=org).values_list("original_filename", flat=True))
    assert {"FI-900.pdf", "Rechnung März 2026.pdf", "invoice-4.pdf"} <= names
    nested = MailboxMessage.objects.get(email__message_id="nested-1@mueller.example")
    outcomes = {a.filename: a.outcome for a in nested.attachments.all()}
    assert outcomes["logo.png"] == "skipped" and outcomes["FI-900.pdf"] == "imported"
    assert outcomes["Rechnung März 2026.pdf"] == "imported" and "positionen.csv" in outcomes   # ingest decides
    mailbox.refresh_from_db()
    assert (mailbox.cursor, mailbox.cursor_validity) == ("6", "1001")
    assert server.selected_readonly == [True] and not server.seen     # read-only, nothing marked

    # Nothing new: the server's "7:*" answer still contains UID 6, which must not be imported again.
    assert imap.poll(mailbox)["emails"] == 0
    server.add("INBOX", 7, simple_email(7, PDF_2))
    stats = imap.poll(mailbox)
    assert stats["emails"] == 1
    assert ("SEARCH", None, "UID", "7:*") in server.commands
    assert MailboxMessage.objects.filter(organization=org).count() == 3
    mailbox.refresh_from_db()
    assert mailbox.cursor == "7" and mailbox.documents_received == len(names) + 1


@pytest.mark.django_db
def test_uidvalidity_change_resets_the_cursor_without_duplicates(org, mailbox, server):
    server.add("INBOX", 4, simple_email(4, PDF_1))
    imap.poll(mailbox)
    # The server rebuilt the folder: new UIDVALIDITY, every message renumbered, one new message.
    server.validity = "2002"
    server.folders["INBOX"] = {1: simple_email(4, PDF_1), 2: simple_email(8, PDF_3)}
    mailbox.refresh_from_db()
    stats = imap.poll(mailbox)
    assert stats["reset"] and stats["emails"] == 1 and stats["already"] == 1
    mailbox.refresh_from_db()
    assert (mailbox.cursor, mailbox.cursor_validity) == ("2", "2002")
    assert AuditEvent.objects.filter(organization=org, action="mailbox.cursor_reset").count() == 1
    assert Document.objects.filter(organization=org).count() == 2


@pytest.mark.django_db
def test_mark_as_read_only_touches_imported_emails(org, mailbox, server):
    mailbox.mark_seen = True
    mailbox.save()
    server.add("INBOX", 1, simple_email(1, PDF_1))
    server.add("INBOX", 2, simple_email(2, None))
    imap.poll(mailbox)
    assert server.selected_readonly == [False] and server.seen == {1}


@pytest.mark.django_db
def test_allowed_senders_apply_to_imap(org, mailbox, server):
    mailbox.allowed_senders = "trusted.example"
    mailbox.save()
    server.add("INBOX", 1, simple_email(1, PDF_1))
    imap.poll(mailbox)
    assert MailboxMessage.objects.get(organization=org).outcome == "blocked"
    assert not Document.objects.filter(organization=org).exists()


# ---------------------------------------------------------------- connection errors


@pytest.mark.django_db
def test_test_connection_gives_specific_errors(org, mailbox, server):
    assert "has 0 emails" in imap.test_connection(mailbox)
    mailbox.password = "wrong"
    with pytest.raises(imap.ImapAuthError, match="rejected the username or password"):
        imap.test_connection(mailbox)
    mailbox.password = "app-password"
    mailbox.host = "nowhere.example"
    with pytest.raises(imap.ImapError, match="Couldn't find the server"):
        imap.test_connection(mailbox)
    mailbox.host = "closed.example"
    with pytest.raises(imap.ImapError, match="refused the connection on port 993"):
        imap.test_connection(mailbox)
    mailbox.host, mailbox.folder = "imap.mail.example", "Invoices 2026"
    with pytest.raises(imap.ImapError, match="no folder named “Invoices 2026”. Folders on this account: INBOX, "
                                             "Archive, Ärger"):
        imap.test_connection(mailbox)


@pytest.mark.django_db
def test_private_network_hosts_are_refused(org, mailbox, monkeypatch, settings):
    settings.MAILBOX_ALLOW_PRIVATE_HOSTS = False
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 0))])
    with pytest.raises(imap.ImapError, match="private network"):
        imap.connect(mailbox)


@pytest.mark.django_db
def test_wrong_password_pauses_checks_until_fixed(client, admin_user, org, mailbox, server):
    from apps.mailboxes.tasks import poll_all_mailboxes

    server.password = "changed"
    stats = poll_mailbox(mailbox)
    mailbox.refresh_from_db()
    assert stats["status"] == "reconnect" and mailbox.needs_reconnect
    assert poll_all_mailboxes()["queued"] == 0          # not retried with a bad password every few minutes

    client.force_login(admin_user)
    r = client.post(reverse("mailboxes:edit", args=[mailbox.pk]), {
        "display_name": "AP inbox", "host": "imap.mail.example", "port": "993", "security": "ssl",
        "username": "ap@acme.example", "password": "changed", "folder": "INBOX"})
    assert r.status_code == 302 and r["Location"] == reverse("mailboxes:index")
    mailbox.refresh_from_db()
    assert not mailbox.needs_reconnect and mailbox.password == "changed"


@pytest.mark.django_db
def test_a_failing_email_is_retried_then_recorded_so_the_folder_keeps_moving(org, mailbox, server, monkeypatch):
    from apps.mailboxes.services import intake

    server.add("INBOX", 1, simple_email(1, PDF_1))
    server.add("INBOX", 2, simple_email(2, PDF_2))
    real = intake.receive

    def flaky(mb, incoming, process="async"):
        if incoming.message_id == "inv-1@harborlink.example":
            raise ValueError("unexpected attachment structure")
        return real(mb, incoming, process=process)

    monkeypatch.setattr(intake, "receive", flaky)
    for _ in range(2):
        assert poll_mailbox(mailbox)["status"] == "error"
        mailbox.refresh_from_db()
        assert mailbox.cursor == ""                      # stopped before the failing email; tried again later
    assert poll_mailbox(mailbox)["status"] == "ok"
    failed = MailboxMessage.objects.get(email__message_id="inv-1@harborlink.example")
    assert failed.outcome == "failed" and "unexpected attachment structure" in failed.note
    assert MailboxMessage.objects.get(email__message_id="inv-2@harborlink.example").outcome == "documents"
