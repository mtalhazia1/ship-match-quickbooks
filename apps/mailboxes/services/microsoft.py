"""Microsoft 365 / Outlook.com mailboxes: OAuth 2.0 authorization code flow and Microsoft Graph polling.

Plain httpx against the documented endpoints (no SDK):
  * https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize and /token, scopes
    "offline_access User.Read Mail.ReadWrite", PKCE (S256) on top of the client secret;
  * access tokens last about an hour; refresh tokens rotate, so the newest one is always stored, and a
    row lock stops two workers from spending the same refresh token;
  * invalid_grant / interaction_required (password changed, consent revoked, refresh token expired)
    marks the mailbox "needs reconnecting", like the QuickBooks connection;
  * Graph throttling (429 / 503 with Retry-After) is honoured; long waits end the check early and the
    next scheduled check continues from the saved cursor.
Polling reads messages with attachments in the chosen folder received since the cursor
(receivedDateTime), oldest first, following @odata.nextLink. File attachments are downloaded from
contentBytes, or from /$value when Graph leaves contentBytes out; attached emails (itemAttachment) and
cloud links (referenceAttachment) are recorded as skipped. Imported emails get the "ShipMatch" category
and/or are moved to a folder, as configured.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from urllib.parse import quote, urlencode

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.utils import audit
from apps.documents.services.ingest import MAX_BYTES as INGEST_MAX_BYTES
from apps.mailboxes.models import Mailbox

from . import intake
from .errors import MailboxAuthError, MailboxError, MailboxThrottled
from .intake import IncomingAttachment, IncomingEmail

log = logging.getLogger(__name__)

AUTHORITY = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access User.Read Mail.ReadWrite"
CATEGORY = "ShipMatch"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
MAX_WAIT_SECONDS = 30          # a longer Retry-After ends this check; the next one continues
PAGE_SIZE = 25
MAX_MESSAGES_PER_CHECK = 100
FIRST_CHECK_DAYS = 7           # a newly connected mailbox imports the last week's emails
AUTH_ERRORS = {"invalid_grant", "interaction_required", "consent_required", "invalid_client", "unauthorized_client"}


class GraphError(MailboxError):
    def __init__(self, message: str, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status, self.code = status, code


class GraphAuthError(MailboxAuthError, GraphError):
    pass


class GraphThrottled(MailboxThrottled, GraphError):
    pass


def configured() -> bool:
    return bool(settings.MS_CLIENT_ID and settings.MS_CLIENT_SECRET and settings.MS_REDIRECT_URI)


def _endpoint(name: str) -> str:
    return f"{AUTHORITY}/{quote(settings.MS_TENANT or 'common', safe='')}/oauth2/v2.0/{name}"


# ---------------------------------------------------------------- OAuth


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_url(state: str, code_challenge: str, login_hint: str = "") -> str:
    params = {
        "client_id": settings.MS_CLIENT_ID, "response_type": "code", "redirect_uri": settings.MS_REDIRECT_URI,
        "response_mode": "query", "scope": SCOPES, "state": state,
        "code_challenge": code_challenge, "code_challenge_method": "S256", "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{_endpoint('authorize')}?{urlencode(params)}"


def _token_request(data: dict, http: httpx.Client | None = None) -> dict:
    client = http or httpx.Client(timeout=30)
    body = {"client_id": settings.MS_CLIENT_ID, "client_secret": settings.MS_CLIENT_SECRET, "scope": SCOPES, **data}
    try:
        r = client.post(_endpoint("token"), data=body, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise GraphError(f"Couldn't reach Microsoft to sign in ({type(e).__name__}). Try again in a few minutes.")
    finally:
        if http is None:
            client.close()
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    if r.status_code >= 400 or "access_token" not in payload:
        code = str(payload.get("error") or "")
        detail = str(payload.get("error_description") or "").split("\r\n")[0].split(" Trace ID")[0][:240]
        if code in AUTH_ERRORS:
            raise GraphAuthError(
                "Microsoft no longer accepts this mailbox's sign-in (the password changed, access was withdrawn or "
                "it wasn't used for a long time). Connect it again in Settings, Email intake."
                + (f" Microsoft said: {detail}" if detail else ""), r.status_code, code)
        raise GraphError(f"Microsoft sign-in failed ({r.status_code} {code}). {detail}".strip(), r.status_code, code)
    return payload


def _granted_mail(token: dict) -> bool:
    scopes = {s.rsplit("/", 1)[-1].lower() for s in str(token.get("scope") or "").split()}
    return not scopes or "mail.readwrite" in scopes


def store_tokens(mailbox: Mailbox, token: dict, save: bool = True) -> Mailbox:
    mailbox.access_token = token["access_token"]
    mailbox.refresh_token = token.get("refresh_token") or mailbox.refresh_token   # always keep the newest
    mailbox.access_expires_at = timezone.now() + timedelta(seconds=int(token.get("expires_in") or 3600))
    mailbox.needs_reconnect = False
    if save and mailbox.pk:
        mailbox.save(update_fields=["access_token", "refresh_token", "access_expires_at", "needs_reconnect",
                                    "updated_at"])
    return mailbox


def exchange_code(code: str, verifier: str, http: httpx.Client | None = None) -> dict:
    return _token_request({"grant_type": "authorization_code", "code": code,
                           "redirect_uri": settings.MS_REDIRECT_URI, "code_verifier": verifier}, http)


def refresh(mailbox: Mailbox, http: httpx.Client | None = None) -> Mailbox:
    """Refresh the access token once across all workers (Microsoft rotates the refresh token)."""
    seen_token = mailbox.access_token
    try:
        with transaction.atomic():
            locked = Mailbox.objects.select_for_update().get(pk=mailbox.pk)
            if locked.access_valid and locked.access_token != seen_token:
                fresh = locked   # another worker refreshed while we waited for the lock
            elif not locked.refresh_token:
                raise GraphAuthError("This mailbox has no saved Microsoft sign-in. Connect it again.")
            else:
                fresh = store_tokens(locked, _token_request(
                    {"grant_type": "refresh_token", "refresh_token": locked.refresh_token}, http))
    except GraphAuthError as e:   # saved outside the rolled-back transaction
        mark_needs_reconnect(mailbox, str(e))
        raise
    for name in ("access_token", "refresh_token", "access_expires_at", "needs_reconnect"):
        setattr(mailbox, name, getattr(fresh, name))
    return mailbox


def mark_needs_reconnect(mailbox: Mailbox, error: str) -> None:
    already = Mailbox.objects.filter(pk=mailbox.pk, needs_reconnect=True).exists()
    Mailbox.objects.filter(pk=mailbox.pk).update(needs_reconnect=True, last_error=error[:1000])
    mailbox.needs_reconnect, mailbox.last_error = True, error
    if not already:
        audit(mailbox.organization, "mailbox.needs_reconnect", mailbox, name=mailbox.label, error=error[:300])


# ---------------------------------------------------------------- Graph client


def _graph_message(r: httpx.Response) -> tuple[str, str]:
    try:
        err = r.json().get("error") or {}
    except ValueError:
        return r.text[:200], ""
    if isinstance(err, dict):
        return str(err.get("message") or "")[:300], str(err.get("code") or "")
    return str(err)[:300], ""


class GraphClient:
    def __init__(self, mailbox: Mailbox, http: httpx.Client | None = None, sleep=time.sleep):
        self.mailbox = mailbox
        self.http = http or httpx.Client(timeout=60)
        self._own_http = http is None
        self.sleep = sleep

    def close(self):
        if self._own_http:
            self.http.close()

    def request(self, method: str, url: str, *, params=None, json=None) -> httpx.Response:
        if not url.startswith("https://"):
            url = GRAPH + url
        refreshed = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if not self.mailbox.access_valid:
                if self.mailbox.pk is None:   # still connecting: there is nothing saved to refresh
                    raise GraphAuthError("Microsoft rejected the new sign-in. Try connecting again.")
                refresh(self.mailbox, self.http)
            headers = {"Authorization": f"Bearer {self.mailbox.access_token}", "Accept": "application/json"}
            try:
                r = self.http.request(method, url, params=params, json=json, headers=headers)
            except httpx.HTTPError as e:
                if attempt == MAX_ATTEMPTS:
                    raise GraphError(f"Couldn't reach Microsoft 365 ({type(e).__name__}). ShipMatch will try again "
                                     "at the next check.")
                self._wait(attempt, None)
                continue
            if r.status_code == 401 and not refreshed:
                self.mailbox.access_expires_at = None   # force a refresh
                refreshed = True
                continue
            if r.status_code in RETRY_STATUSES:
                if attempt == MAX_ATTEMPTS:
                    raise GraphThrottled("Microsoft 365 is busy or limiting requests for this mailbox. ShipMatch will "
                                         "continue at the next check.", r.status_code)
                self._wait(attempt, r.headers.get("Retry-After"))
                continue
            if r.status_code >= 400:
                message, code = _graph_message(r)
                if r.status_code == 401:
                    raise GraphAuthError("Microsoft 365 rejected the saved sign-in. Connect the mailbox again.",
                                         r.status_code, code)
                if r.status_code == 404 or code == "ErrorItemNotFound":
                    raise GraphError(f"Microsoft 365 couldn't find this item ({message or code}). If you deleted or "
                                     "renamed the folder, choose another one in the mailbox settings.", 404, code)
                if r.status_code == 403:
                    raise GraphError(f"Microsoft 365 refused access ({message or code}). The account may not have a "
                                     "mailbox, or an admin may have blocked ShipMatch.", 403, code)
                raise GraphError(f"Microsoft 365 returned an error ({r.status_code} {code}): {message}".strip(),
                                 r.status_code, code)
            return r
        raise GraphError("Microsoft 365 didn't answer after several attempts.")

    def _wait(self, attempt: int, retry_after: str | None) -> None:
        try:
            wait = float(retry_after) if retry_after else 0.0
        except ValueError:
            wait = 0.0
        if wait > MAX_WAIT_SECONDS:
            raise GraphThrottled(f"Microsoft 365 asked ShipMatch to wait {int(wait)} seconds before reading this "
                                 "mailbox again. The next check continues where this one stopped.", 429)
        self.sleep(max(wait, min(8.0, 2.0 ** attempt)))

    def get(self, url: str, params=None) -> dict:
        return self.request("GET", url, params=params).json()

    def paged(self, url: str, params=None):
        """Every item of a collection, following @odata.nextLink (it already carries the query)."""
        while url:
            data = self.get(url, params)
            params = None
            yield from data.get("value") or []
            url = data.get("@odata.nextLink") or ""

    # ---- mailbox helpers

    def me(self) -> dict:
        return self.get("/me", {"$select": "id,displayName,mail,userPrincipalName"})

    def folder(self, folder_id: str) -> dict:
        return self.get(f"/me/mailFolders/{quote(folder_id, safe='')}", {"$select": "id,displayName"})

    def folders(self) -> list[dict]:
        """Top-level folders and their direct subfolders, as [{"id", "name"}] sorted for a select list."""
        out = []
        params = {"$top": 100, "$select": "id,displayName,childFolderCount"}
        for f in self.paged("/me/mailFolders", dict(params)):
            out.append({"id": f["id"], "name": f.get("displayName") or "Folder"})
            if f.get("childFolderCount"):
                for c in self.paged(f"/me/mailFolders/{quote(f['id'], safe='')}/childFolders", dict(params)):
                    out.append({"id": c["id"], "name": f"{f.get('displayName')}/{c.get('displayName')}"})
        return sorted(out, key=lambda f: (f["name"].lower() != "inbox", f["name"].lower()))


# ---------------------------------------------------------------- connect


def connect_mailbox(org, code: str, verifier: str, actor=None, existing: Mailbox | None = None,
                    http: httpx.Client | None = None) -> tuple[Mailbox, bool]:
    """Finish the OAuth flow: store tokens on a new or existing mailbox. Returns (mailbox, created)."""
    token = exchange_code(code, verifier, http)
    if not _granted_mail(token):
        raise GraphError("Microsoft didn't give ShipMatch access to email. Ask your Microsoft 365 admin to approve "
                         "the ShipMatch app (Mail.ReadWrite), then connect again.")
    probe = Mailbox(organization=org, kind=Mailbox.Kind.MICROSOFT)
    store_tokens(probe, token, save=False)
    client = GraphClient(probe, http)
    try:
        me = client.me()
        address = (me.get("mail") or me.get("userPrincipalName") or "").strip()
        if not address:
            raise GraphError("This Microsoft account has no mailbox. Sign in with an account that receives email.")
        if existing and existing.address and existing.address.lower() != address.lower():
            raise GraphError(f"You signed in as {address}, but this mailbox is {existing.address}. Sign in with "
                             f"{existing.address}, or add {address} as a new mailbox.")
        mailbox = existing or (Mailbox.objects.filter(organization=org, kind=Mailbox.Kind.MICROSOFT,
                                                      address__iexact=address).first())
        created = mailbox is None
        if created:
            inbox = client.folder("inbox")
            mailbox = Mailbox(
                organization=org, kind=Mailbox.Kind.MICROSOFT, address=address[:320],
                display_name=(me.get("displayName") or address)[:120], folder=inbox["id"],
                folder_name=inbox.get("displayName") or "Inbox",
                cursor=(timezone.now() - timedelta(days=FIRST_CHECK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                created_by=actor if getattr(actor, "is_authenticated", False) else None)
        mailbox.address = address[:320]
        store_tokens(mailbox, token, save=False)
        try:
            mailbox.folder_options = client.folders()
        except GraphError:
            log.warning("Couldn't list folders for %s", address, exc_info=True)
        mailbox.last_error = ""
        mailbox.save()
    finally:
        client.close()
    return mailbox, created


def refresh_folders(mailbox: Mailbox, http: httpx.Client | None = None) -> list[dict]:
    client = GraphClient(mailbox, http)
    try:
        mailbox.folder_options = client.folders()
    finally:
        client.close()
    mailbox.save(update_fields=["folder_options", "updated_at"])
    return mailbox.folder_options


def test_connection(mailbox: Mailbox, http: httpx.Client | None = None) -> str:
    client = GraphClient(mailbox, http)
    try:
        me = client.me()
        folder = client.folder(mailbox.folder or "inbox")
    finally:
        client.close()
    who = me.get("mail") or me.get("userPrincipalName") or mailbox.address
    return f"Connected to {who}. ShipMatch reads the folder “{folder.get('displayName') or mailbox.folder_name}”."


# ---------------------------------------------------------------- polling


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _attachments(client: GraphClient, message_id: str) -> list[IncomingAttachment]:
    out = []
    base = f"/me/messages/{quote(message_id, safe='')}/attachments"
    for a in client.paged(base):
        kind = str(a.get("@odata.type") or "")
        name = str(a.get("name") or "")
        size = int(a.get("size") or 0)
        ctype = str(a.get("contentType") or "application/octet-stream")
        if kind.endswith("itemAttachment"):
            out.append(IncomingAttachment(name or "Attached email", "message/rfc822", None, size,
                                          skip_reason="An attached email or calendar item. Forward that email to "
                                                      "ShipMatch on its own instead."))
            continue
        if kind.endswith("referenceAttachment"):
            out.append(IncomingAttachment(name or "Linked file", ctype, None, size,
                                          skip_reason="A link to a file in OneDrive or SharePoint, not the file "
                                                      "itself. Attach the file instead of sharing a link."))
            continue
        if size > INGEST_MAX_BYTES:
            out.append(IncomingAttachment(name, ctype, None, size, skip_reason=(
                f"Larger than {INGEST_MAX_BYTES // (1024 * 1024)} MB")))
            continue
        encoded = a.get("contentBytes")
        if encoded:
            try:
                content = base64.b64decode(encoded)
            except ValueError:
                content = None
        else:   # Graph leaves contentBytes out of some large attachments: download the raw file
            content = client.request("GET", f"{base}/{quote(str(a.get('id')), safe='')}/$value").content
        out.append(IncomingAttachment(name, ctype, content, len(content) if content is not None else size,
                                      inline=bool(a.get("isInline")),
                                      skip_reason="" if content is not None else "Couldn't decode this attachment"))
    return out


def _mark_processed(client: GraphClient, mailbox: Mailbox, message: dict) -> str:
    """Category and/or move. Returns a warning (or "") instead of failing the import."""
    action = mailbox.after_import
    url = f"/me/messages/{quote(message['id'], safe='')}"
    try:
        if action in (Mailbox.AfterImport.CATEGORY, Mailbox.AfterImport.CATEGORY_MOVE):
            categories = list(message.get("categories") or [])
            if CATEGORY not in categories:
                client.request("PATCH", url, json={"categories": [*categories, CATEGORY]})
        if action in (Mailbox.AfterImport.MOVE, Mailbox.AfterImport.CATEGORY_MOVE) and mailbox.processed_folder:
            if mailbox.processed_folder != mailbox.folder:
                client.request("POST", f"{url}/move", json={"destinationId": mailbox.processed_folder})
    except MailboxThrottled:
        raise
    except GraphError as e:
        return f"Imported, but couldn't mark the email in Outlook: {e}"
    return ""


def poll(mailbox: Mailbox, process: str = "async", http: httpx.Client | None = None, sleep=time.sleep) -> dict:
    org = mailbox.organization
    stats = {"emails": 0, "documents": 0, "already": 0, "skipped_attachments": 0, "warnings": []}
    since = _parse_time(mailbox.cursor) or (timezone.now() - timedelta(days=FIRST_CHECK_DAYS))
    newest = since
    params = {
        "$filter": f"receivedDateTime ge {_iso(since)} and hasAttachments eq true",
        "$orderby": "receivedDateTime asc",
        "$select": "id,internetMessageId,subject,from,receivedDateTime,categories",
        "$top": PAGE_SIZE,
    }
    client = GraphClient(mailbox, http, sleep)
    try:
        folder = quote(mailbox.folder or "inbox", safe="")
        for n, m in enumerate(client.paged(f"/me/mailFolders/{folder}/messages", params)):
            if n >= MAX_MESSAGES_PER_CHECK:
                stats["more"] = True
                break
            received = _parse_time(m.get("receivedDateTime")) or newest
            key = intake.message_key(m.get("internetMessageId"), "graph", mailbox.address, m.get("id"))
            if intake.already_received(org, key):
                stats["already"] += 1
            else:
                sender = (m.get("from") or {}).get("emailAddress") or {}
                incoming = IncomingEmail(
                    message_id=key, subject=str(m.get("subject") or "")[:500],
                    sender=f"{sender.get('name') or ''} <{sender.get('address') or ''}>".strip(),
                    received_at=received, recipient=mailbox.address, provider_ref=str(m.get("id"))[:255])
                if mailbox.sender_allowed(incoming.sender_address):
                    incoming.attachments = _attachments(client, m["id"])
                result = intake.receive_or_give_up(mailbox, incoming, process=process)
                if result.created:
                    stats["emails"] += 1
                    stats["documents"] += len(result.new_documents)
                    stats["skipped_attachments"] += result.skipped
                    warning = _mark_processed(client, mailbox, m)
                    if warning:
                        stats["warnings"].append(warning)
            if received > newest:
                newest = received
                Mailbox.objects.filter(pk=mailbox.pk).update(cursor=_iso(newest))
                mailbox.cursor = _iso(newest)
    finally:
        client.close()
    return stats
