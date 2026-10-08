"""Microsoft 365: OAuth connect, token refresh and rotation, Graph polling with paging, throttling and errors.

Microsoft is never called: httpx.MockTransport plays login.microsoftonline.com and graph.microsoft.com.
"""
import base64
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuditEvent
from apps.documents.models import Document
from apps.mailboxes.models import Mailbox, MailboxMessage
from apps.mailboxes.services import microsoft
from apps.mailboxes.services.polling import poll_mailbox

from .pdfs import make_pdf

PDF_A = make_pdf("commercial invoice CI-55")
PDF_B = make_pdf("freight invoice FI-56")
PDF_BIG = make_pdf("scan") + b"\n%" + b"x" * 5000 + b"\n"   # a real PDF, padded to a few KB (comments after %%EOF are legal)
LOGO = b"\x89PNG" + b"\x00" * 3000


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def message(mid, when, subject, sender="billing@harborlink.example"):
    return {"id": mid, "internetMessageId": f"<{mid}@outlook.example>", "subject": subject,
            "from": {"emailAddress": {"name": "Harborlink", "address": sender}},
            "receivedDateTime": when, "categories": ["Blue"]}


class FakeMicrosoft:
    """Just enough of the identity platform and Graph to drive the code paths."""

    def __init__(self):
        self.calls, self.token_requests, self.patches, self.moves, self.sleeps = [], [], [], [], []
        self.refresh_n = 0
        self.fail_refresh = False
        self.throttle_messages = 0
        self.retry_after = "2"
        self.expire_access_once = False
        self.messages = [
            message("m1", "2026-10-01T08:00:00Z", "Invoice CI-55"),
            message("m2", "2026-10-01T09:30:00Z", "Freight invoice FI-56"),
            message("m3", "2026-10-02T07:00:00Z", "Large scan"),
        ]
        self.attachments = {
            "m1": [
                {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a1", "name": "CI-55.pdf",
                 "contentType": "application/pdf", "size": len(PDF_A), "isInline": False, "contentBytes": b64(PDF_A)},
                {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a2", "name": "image001.png",
                 "contentType": "image/png", "size": len(LOGO), "isInline": True, "contentBytes": b64(LOGO)},
                {"@odata.type": "#microsoft.graph.itemAttachment", "id": "a3", "name": "RE: booking",
                 "contentType": None, "size": 9000, "isInline": False},
            ],
            "m2": [{"@odata.type": "#microsoft.graph.fileAttachment", "id": "b1", "name": "FI-56.pdf",
                    "contentType": "application/pdf", "size": len(PDF_B), "isInline": False,
                    "contentBytes": b64(PDF_B)}],
            "m3": [{"@odata.type": "#microsoft.graph.fileAttachment", "id": "c1", "name": "scan.pdf",
                    "contentType": "application/pdf", "size": len(PDF_BIG), "isInline": False}],   # no contentBytes
        }

    # ---- identity platform
    def token(self, request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_requests.append(form)
        assert form["client_id"] == "ms-client" and form["client_secret"] == "ms-secret"
        assert "Mail.ReadWrite" in form["scope"] and "offline_access" in form["scope"]
        if form["grant_type"] == "authorization_code":
            assert form["code"] == "auth-code" and form["code_verifier"]
            return httpx.Response(200, json={"access_token": "at-0", "refresh_token": "rt-0", "expires_in": 3600,
                                             "scope": "https://graph.microsoft.com/Mail.ReadWrite User.Read"})
        assert form["grant_type"] == "refresh_token"
        if self.fail_refresh:
            return httpx.Response(400, json={"error": "invalid_grant",
                                             "error_description": "AADSTS700082: The refresh token has expired."})
        assert form["refresh_token"] == f"rt-{self.refresh_n}"
        self.refresh_n += 1
        return httpx.Response(200, json={"access_token": f"at-{self.refresh_n}", "refresh_token": f"rt-{self.refresh_n}",
                                         "expires_in": 3600})

    # ---- Graph
    def graph(self, request):
        path = request.url.path.removeprefix("/v1.0")
        self.calls.append((request.method, path, dict(request.url.params)))
        if self.expire_access_once:
            self.expire_access_once = False
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken", "message": "expired"}})
        if path == "/me":
            return httpx.Response(200, json={"displayName": "Acme AP", "mail": "ap@acme.example",
                                             "userPrincipalName": "ap@acme.example"})
        if path == "/me/mailFolders/inbox":
            return httpx.Response(200, json={"id": "INBOX-ID", "displayName": "Inbox"})
        if path == "/me/mailFolders/INBOX-ID":
            return httpx.Response(200, json={"id": "INBOX-ID", "displayName": "Inbox"})
        if path == "/me/mailFolders/GONE":
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound", "message": "Not found"}})
        if path == "/me/mailFolders":
            return httpx.Response(200, json={"value": [
                {"id": "INBOX-ID", "displayName": "Inbox", "childFolderCount": 1},
                {"id": "ARCH-ID", "displayName": "Archive", "childFolderCount": 0}]})
        if path == "/me/mailFolders/INBOX-ID/childFolders":
            return httpx.Response(200, json={"value": [{"id": "INV-ID", "displayName": "Invoices", "childFolderCount": 0}]})
        if path == "/me/mailFolders/INBOX-ID/messages":
            if self.throttle_messages:
                self.throttle_messages -= 1
                return httpx.Response(429, headers={"Retry-After": self.retry_after},
                                      json={"error": {"code": "TooManyRequests"}})
            since = request.url.params.get("$filter", "").split("ge ")[1].split(" ")[0]
            msgs = [m for m in self.messages if m["receivedDateTime"] >= since]
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json={"value": msgs[2:]})
            body = {"value": msgs[:2]}
            if len(msgs) > 2:   # page 2 is reached through @odata.nextLink only
                body["@odata.nextLink"] = "https://graph.microsoft.com/v1.0/me/mailFolders/INBOX-ID/messages?" \
                                          f"page=2&%24filter=receivedDateTime+ge+{since}"
            return httpx.Response(200, json=body)
        if path.startswith("/me/messages/") and path.endswith("/attachments"):
            mid = path.split("/")[3]
            return httpx.Response(200, json={"value": self.attachments[mid]})
        if path == "/me/messages/m3/attachments/c1/$value":
            return httpx.Response(200, content=PDF_BIG, headers={"Content-Type": "application/pdf"})
        if request.method == "PATCH" and path.startswith("/me/messages/"):
            self.patches.append((path.split("/")[3], json.loads(request.content)))
            return httpx.Response(200, json={})
        if request.method == "POST" and path.endswith("/move"):
            self.moves.append((path.split("/")[3], json.loads(request.content)))
            return httpx.Response(201, json={"id": "moved"})
        return httpx.Response(404, json={"error": {"code": "NotFound", "message": path}})

    def handler(self, request):
        if request.url.host == "login.microsoftonline.com":
            assert request.url.path == "/common/oauth2/v2.0/token"
            return self.token(request)
        assert request.url.host == "graph.microsoft.com"
        assert request.headers["Authorization"].startswith("Bearer at-")
        return self.graph(request)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture(autouse=True)
def _ms_settings(settings):
    settings.MS_CLIENT_ID, settings.MS_CLIENT_SECRET, settings.MS_TENANT = "ms-client", "ms-secret", "common"
    settings.MS_REDIRECT_URI = "http://testserver/settings/email/microsoft/callback"


@pytest.fixture
def fake():
    return FakeMicrosoft()


@pytest.fixture
def patched_http(fake, monkeypatch):
    """Code that creates its own httpx.Client gets one wired to the fake."""
    real = httpx.Client

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real(transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(httpx, "Client", factory)
    return fake


@pytest.fixture
def mailbox(org):
    return Mailbox.objects.create(
        organization=org, kind=Mailbox.Kind.MICROSOFT, address="ap@acme.example", display_name="Acme AP",
        access_token="at-0", refresh_token="rt-0", access_expires_at=timezone.now() + timedelta(hours=1),
        folder="INBOX-ID", folder_name="Inbox", cursor="2026-09-30T00:00:00Z")


# ---------------------------------------------------------------- OAuth connect


@pytest.mark.django_db
def test_connect_flow_checks_state_and_stores_encrypted_tokens(client, admin_user, org, patched_http):
    client.force_login(admin_user)
    r = client.get(reverse("mailboxes:microsoft_connect"))
    assert r.status_code == 302
    url = urlparse(r["Location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "login.microsoftonline.com" and url.path == "/common/oauth2/v2.0/authorize"
    assert q["scope"] == "offline_access User.Read Mail.ReadWrite" and q["response_type"] == "code"
    assert q["code_challenge_method"] == "S256" and q["redirect_uri"].endswith("/microsoft/callback")

    # A forged or replayed callback is refused before any token request.
    bad = client.get(reverse("mailboxes:microsoft_callback"), {"state": "forged", "code": "auth-code"}, follow=True)
    assert "expired or was opened twice" in bad.content.decode()
    assert not Mailbox.objects.filter(kind="microsoft").exists() and not patched_http.token_requests

    r = client.get(reverse("mailboxes:microsoft_connect"))
    state = parse_qs(urlparse(r["Location"]).query)["state"][0]
    r = client.get(reverse("mailboxes:microsoft_callback"), {"state": state, "code": "auth-code"})
    mailbox = Mailbox.objects.get(organization=org, kind="microsoft")
    assert r.status_code == 302 and r["Location"] == reverse("mailboxes:edit", args=[mailbox.pk])
    assert mailbox.address == "ap@acme.example" and mailbox.folder == "INBOX-ID" and mailbox.folder_name == "Inbox"
    assert mailbox.access_token == "at-0" and mailbox.refresh_token == "rt-0"
    assert [f["name"] for f in mailbox.folder_options] == ["Inbox", "Archive", "Inbox/Invoices"]
    with connection.cursor() as c:   # stored encrypted at rest
        c.execute("select access_token, refresh_token from mailboxes_mailbox where id = %s", [mailbox.pk])
        raw = c.fetchone()
    assert "at-0" not in raw[0] and "rt-0" not in raw[1]
    assert AuditEvent.objects.filter(organization=org, action="mailbox.connected").exists()
    # The same state can't be used twice.
    again = client.get(reverse("mailboxes:microsoft_callback"), {"state": state, "code": "auth-code"}, follow=True)
    assert "expired or was opened twice" in again.content.decode()


@pytest.mark.django_db
def test_connect_cancelled_by_user(client, admin_user, org, patched_http):
    client.force_login(admin_user)
    r = client.get(reverse("mailboxes:microsoft_connect"))
    state = parse_qs(urlparse(r["Location"]).query)["state"][0]
    r = client.get(reverse("mailboxes:microsoft_callback"),
                   {"state": state, "error": "access_denied", "error_description": "The user cancelled"}, follow=True)
    assert "declined access" in r.content.decode()
    assert not Mailbox.objects.filter(kind="microsoft").exists()


@pytest.mark.django_db
def test_connect_requires_configuration_and_manage_permission(client, admin_user, user, settings):
    client.force_login(user)
    assert client.get(reverse("mailboxes:microsoft_connect")).status_code == 403
    client.force_login(admin_user)
    assert client.get(reverse("mailboxes:microsoft_connect"), {"mailbox": "x"}).status_code == 302
    assert client.get(reverse("mailboxes:microsoft_connect"), {"mailbox": "999"}).status_code == 404
    settings.MS_CLIENT_ID = ""
    r = client.get(reverse("mailboxes:microsoft_connect"), follow=True)
    assert "isn&#x27;t set up on this server" in r.content.decode()


# ---------------------------------------------------------------- polling


@pytest.mark.django_db
def test_poll_pages_downloads_and_marks_processed(org, mailbox, fake):
    stats = microsoft.poll(mailbox, http=fake.client(), sleep=fake.sleeps.append)
    assert stats["emails"] == 3 and stats["documents"] == 3
    names = sorted(Document.objects.filter(organization=org).values_list("original_filename", flat=True))
    assert names == ["CI-55.pdf", "FI-56.pdf", "scan.pdf"]
    assert Document.objects.get(original_filename="scan.pdf").file.read() == PDF_BIG   # via /$value

    m1 = MailboxMessage.objects.get(email__message_id="m1@outlook.example")
    outcomes = {a.filename: a.outcome for a in m1.attachments.all()}
    assert outcomes == {"CI-55.pdf": "imported", "image001.png": "skipped", "RE: booking": "skipped"}
    assert "Forward that email" in m1.attachments.get(filename="RE: booking").reason

    # Paging followed @odata.nextLink; the filter asked only for messages with attachments since the cursor.
    listing = [c for c in fake.calls if c[1] == "/me/mailFolders/INBOX-ID/messages"]
    assert len(listing) == 2 and "hasAttachments eq true" in listing[0][2]["$filter"]
    assert listing[0][2]["$orderby"] == "receivedDateTime asc"
    # Each imported email got the category, keeping the ones it had.
    assert fake.patches[0] == ("m1", {"categories": ["Blue", "ShipMatch"]}) and len(fake.patches) == 3

    mailbox.refresh_from_db()
    assert mailbox.cursor == "2026-10-02T07:00:00Z"
    again = microsoft.poll(mailbox, http=fake.client(), sleep=fake.sleeps.append)
    assert again["emails"] == 0 and again["already"] == 1     # the boundary email is seen, never imported twice
    assert MailboxMessage.objects.filter(organization=org).count() == 3


@pytest.mark.django_db
def test_poll_moves_emails_when_configured(org, mailbox, fake):
    mailbox.after_import, mailbox.processed_folder = Mailbox.AfterImport.MOVE, "ARCH-ID"
    mailbox.save()
    microsoft.poll(mailbox, http=fake.client(), sleep=fake.sleeps.append)
    assert fake.moves[0] == ("m1", {"destinationId": "ARCH-ID"}) and not fake.patches


@pytest.mark.django_db
def test_throttling_honours_retry_after(org, mailbox, fake):
    fake.throttle_messages = 1
    stats = microsoft.poll(mailbox, http=fake.client(), sleep=fake.sleeps.append)
    assert fake.sleeps == [2.0] and stats["emails"] == 3


@pytest.mark.django_db
def test_long_throttle_ends_the_check_and_keeps_the_cursor(org, mailbox, fake, patched_http):
    fake.throttle_messages, fake.retry_after = 1, "300"
    stats = poll_mailbox(mailbox)
    mailbox.refresh_from_db()
    assert stats["status"] == "error" and "wait 300 seconds" in stats["error"]
    assert mailbox.cursor == "2026-09-30T00:00:00Z" and not mailbox.needs_reconnect and mailbox.last_error


@pytest.mark.django_db
def test_expired_access_token_is_refreshed_and_rotated(org, mailbox, fake):
    mailbox.access_expires_at = timezone.now() - timedelta(minutes=5)
    mailbox.save()
    microsoft.poll(mailbox, http=fake.client(), sleep=fake.sleeps.append)
    mailbox.refresh_from_db()
    assert [r["grant_type"] for r in fake.token_requests] == ["refresh_token"]
    assert mailbox.access_token == "at-1" and mailbox.refresh_token == "rt-1"   # newest refresh token kept


@pytest.mark.django_db
def test_401_from_graph_refreshes_once_and_retries(org, mailbox, fake):
    fake.expire_access_once = True
    stats = microsoft.poll(mailbox, http=fake.client(), sleep=fake.sleeps.append)
    assert stats["emails"] == 3 and len(fake.token_requests) == 1


@pytest.mark.django_db
def test_invalid_grant_marks_needs_reconnect(client, admin_user, org, mailbox, fake, patched_http):
    fake.fail_refresh = True
    mailbox.access_expires_at = timezone.now() - timedelta(minutes=5)
    mailbox.save()
    stats = poll_mailbox(mailbox)
    mailbox.refresh_from_db()
    assert stats["status"] == "reconnect" and mailbox.needs_reconnect
    assert "AADSTS700082" in mailbox.last_error
    assert AuditEvent.objects.filter(organization=org, action="mailbox.needs_reconnect").count() == 1
    poll_mailbox(mailbox)   # still broken: no second audit row
    assert AuditEvent.objects.filter(organization=org, action="mailbox.needs_reconnect").count() == 1
    client.force_login(admin_user)
    page = client.get(reverse("mailboxes:index")).content.decode()
    assert "needs to be connected again" in page and f"?mailbox={mailbox.pk}" in page


@pytest.mark.django_db
def test_missing_folder_gives_a_specific_error(org, mailbox, fake):
    mailbox.folder = "GONE"
    with pytest.raises(microsoft.GraphError, match="choose another one"):
        microsoft.test_connection(mailbox, http=fake.client())
    mailbox.folder = "INBOX-ID"
    assert "ap@acme.example" in microsoft.test_connection(mailbox, http=fake.client())


@pytest.mark.django_db
def test_settings_form_requires_a_move_folder(client, admin_user, org, mailbox):
    mailbox.folder_options = [{"id": "INBOX-ID", "name": "Inbox"}, {"id": "ARCH-ID", "name": "Archive"}]
    mailbox.save()
    client.force_login(admin_user)
    url = reverse("mailboxes:edit", args=[mailbox.pk])
    r = client.post(url, {"folder": "INBOX-ID", "after_import": "move", "processed_folder": ""})
    assert r.status_code == 200 and "Choose the folder imported emails should move to" in r.content.decode()
    r = client.post(url, {"folder": "ARCH-ID", "after_import": "category_move", "processed_folder": "INBOX-ID",
                          "allowed_senders": "harborlink.example"})
    assert r.status_code == 302
    mailbox.refresh_from_db()
    assert (mailbox.folder, mailbox.folder_name, mailbox.processed_folder_name) == ("ARCH-ID", "Archive", "Inbox")
    assert mailbox.sender_rules() == ["harborlink.example"]
