"""Xero Accounting API client: OAuth 2.0, organisations (tenants), contacts, accounts, bills (ACCPAY invoices),
credit notes (ACCPAYCREDIT), attachments and payment status.

Plain httpx against the documented endpoints (no SDK):
  * sign-in: authorization code flow at https://login.xero.com/identity/connect/authorize, tokens from
    https://identity.xero.com/connect/token. With XERO_CLIENT_SECRET set the app authenticates with its
    secret (a "Web app" in the Xero developer portal); without one it uses PKCE (S256), for a PKCE app.
    The `state` value is checked on the way back;
  * access tokens last 30 minutes; refresh tokens rotate on every use (always store the newest) and expire
    after 60 days without use. A row lock stops two workers from spending the same refresh token;
    `invalid_grant` marks the connection "needs reconnecting";
  * one sign-in can reach several Xero organisations: https://api.xero.com/connections lists them, and
    every API call names the chosen one in the `Xero-tenant-id` header;
  * limits per organisation: 60 calls a minute, 5 at a time and a daily allowance. A 429 carries
    `Retry-After` and `X-Rate-Limit-Problem` (minute, day, concurrent, appminute). Short waits are slept
    through; a used-up daily allowance stops the run with a clear message;
  * writes carry an `Idempotency-Key` (128 characters at most): a retry with the same key returns the first
    result instead of creating a second bill;
  * validation errors come back as Elements[].ValidationErrors[].Message and are shown in plain words.
Disconnecting revokes the refresh token at https://identity.xero.com/connect/revocation, which removes the
app's access to every organisation that sign-in connected.

Nothing here runs on a public demo (DEMO_MODE without DEMO_SEND_OUTSIDE): it would send data outside.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import re
import secrets
import time
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounting.models import XeroConnection

log = logging.getLogger(__name__)

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
REVOKE_URL = "https://identity.xero.com/connect/revocation"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"
RETRY_STATUSES = {500, 502, 503, 504}
MAX_ATTEMPTS = 4
MAX_WAIT_SECONDS = 60          # a longer Retry-After (the daily allowance) stops the run instead of sleeping
ACCESS_SECONDS = 1800          # Xero access tokens last 30 minutes
REFRESH_DAYS = 60              # a refresh token not used for 60 days expires
IDS_PER_CALL = 40              # invoice IDs per GET (keeps the URL short)
CREDIT_IDS_PER_CALL = 10       # credit notes are read with a where filter, which is longer per ID
EXPENSE_TYPES = {"EXPENSE": "Expense", "DIRECTCOSTS": "Direct costs", "OVERHEADS": "Overhead"}
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
DEMO_BLOCKED = ("This is the public demo, so nothing is sent to Xero. On a private demo set DEMO_SEND_OUTSIDE=1 "
                "to connect a Xero demo company.")
RECONNECT = "An admin can connect it again in Settings > Accounting."


class XeroError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = "", code: str = ""):
        super().__init__(message)
        self.status, self.body, self.code = status, body, code


class XeroAuthError(XeroError):
    """The refresh token was rejected, or Xero no longer lets ShipMatch into the organisation. Reconnect."""


class XeroRateLimited(XeroError):
    def __init__(self, message: str, retry_after: float = 0.0, problem: str = "", status: int | None = 429):
        super().__init__(message, status)
        self.retry_after, self.problem = retry_after, problem

    @property
    def daily(self) -> bool:
        return self.problem == "day" or self.retry_after > MAX_WAIT_SECONDS


class XeroDailyLimit(XeroRateLimited):
    """The organisation's daily allowance of Xero API calls is used up: stop now, try again later."""


class XeroValidationError(XeroError):
    def __init__(self, messages: list[str], status: int | None = 400, body: str = ""):
        self.messages = [m for m in messages if m] or ["Xero didn't say what was wrong."]
        super().__init__(plain_validation(self.messages), status, body, code="validation")


class XeroNotFound(XeroError):
    pass


def configured() -> bool:
    return bool(settings.XERO_CLIENT_ID and settings.XERO_REDIRECT_URI)


def uses_pkce() -> bool:
    """No client secret configured: the Xero app is a PKCE app, so the code exchange proves a verifier."""
    return not settings.XERO_CLIENT_SECRET


def _blocked() -> bool:
    from apps.demo.mail import outbound_blocked

    return outbound_blocked()


# --------------------------------------------------------------------------- plain words


# Xero's validation messages that a bookkeeper can act on, by a fragment of the message.
FRIENDLY = [
    (re.compile(r"account code '?([^' ]*)'? is not a valid code|account code .* (?:is )?archived|"
                r"accountcode .* not valid|account .* cannot be used", re.I),
     "Xero won't accept that expense account on a bill (it doesn't exist, is archived, or isn't an expense "
     "account). Choose another account in Settings > Accounting, or change the vendor's account rule."),
    (re.compile(r"not subscribed to currency|currency .* (?:is not|isn't) (?:valid|enabled)|invalid currency", re.I),
     "Xero doesn't have this currency turned on. Add it in Xero under Settings > Currencies (needs a plan with "
     "multi-currency), then post again."),
    (re.compile(r"due ?date", re.I),
     "Xero needs a due date to approve the bill. Add the due date in the review screen, or post bills as drafts "
     "(Settings > Accounting)."),
    (re.compile(r"lock date|period lock|end of year lock", re.I),
     "The invoice date falls in a period that is locked in Xero. Ask your accountant to move the lock date, or "
     "correct the invoice date."),
    (re.compile(r"contact .* archived|archived contact", re.I),
     "The vendor's contact is archived in Xero. Restore it in Xero (Contacts, Archived) and post again."),
    (re.compile(r"total .* (?:negative|less than zero)|cannot be negative", re.I),
     "Xero won't accept a bill whose total is below zero. Check the amounts in the review screen."),
]


def plain_validation(messages: list[str]) -> str:
    hints = []
    for pattern, hint in FRIENDLY:
        if any(pattern.search(m) for m in messages) and hint not in hints:
            hints.append(hint)
    said = "; ".join(dict.fromkeys(m.strip().rstrip(".") for m in messages))[:600]
    return (" ".join(hints) + " " if hints else "") + f"Xero said: {said}."


def _messages(data) -> list[str]:
    if not isinstance(data, dict):
        return []
    out = []
    for element in data.get("Elements") or []:
        for v in element.get("ValidationErrors") or []:
            out.append(str(v.get("Message") or ""))
        for key in ("LineItems", "Allocations", "Payments"):
            for sub in element.get(key) or []:
                for v in (sub or {}).get("ValidationErrors") or []:
                    out.append(str(v.get("Message") or ""))
    for v in data.get("ValidationErrors") or []:
        out.append(str(v.get("Message") or ""))
    return [m for m in out if m]


def _error(r: httpx.Response) -> XeroError:
    try:
        data = r.json()
    except ValueError:
        data = None
    body = r.text[:1000]
    if r.status_code == 400:
        found = _messages(data)
        if found:
            return XeroValidationError(found, r.status_code, body)
    if r.status_code in (401, 403):
        detail = str((data or {}).get("Detail") or (data or {}).get("Title") or "") if isinstance(data, dict) else ""
        return XeroAuthError("Xero no longer lets ShipMatch into this organisation (the app was disconnected in Xero "
                             f"or the sign-in lost access). {RECONNECT}", r.status_code, body, code=detail[:60])
    if r.status_code == 404:
        return XeroNotFound("Xero couldn't find it (it may have been deleted).", r.status_code, body)
    if r.status_code == 413:
        return XeroError("The PDF is larger than Xero accepts for an attachment. Attach a smaller copy in Xero by hand.",
                         r.status_code, body)
    message = ""
    if isinstance(data, dict):
        message = str(data.get("Message") or data.get("Detail") or data.get("Title") or "")
    return XeroError(f"Xero rejected the request ({r.status_code}): {message or r.text[:200] or 'no reason given'}",
                     r.status_code, body)


# --------------------------------------------------------------------------- OAuth


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_url(state: str, code_challenge: str | None = None) -> str:
    params = {"response_type": "code", "client_id": settings.XERO_CLIENT_ID, "redirect_uri": settings.XERO_REDIRECT_URI,
              "scope": settings.XERO_SCOPES, "state": state}
    if code_challenge:
        params.update({"code_challenge": code_challenge, "code_challenge_method": "S256"})
    return f"{AUTH_URL}?{urlencode(params)}"


def _token_request(data: dict, http: httpx.Client | None = None) -> dict:
    if _blocked():
        raise XeroError(DEMO_BLOCKED)
    body = dict(data)
    auth = None
    if settings.XERO_CLIENT_SECRET:
        auth = (settings.XERO_CLIENT_ID, settings.XERO_CLIENT_SECRET)
    else:
        body["client_id"] = settings.XERO_CLIENT_ID
    client = http or httpx.Client(timeout=30)
    try:
        r = client.post(TOKEN_URL, data=body, auth=auth, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise XeroError(f"Couldn't reach Xero to sign in ({type(e).__name__}). Try again in a few minutes.") from e
    finally:
        if http is None:
            client.close()
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    if r.status_code >= 400 or "access_token" not in payload:
        code = str(payload.get("error") or "")
        if code == "invalid_grant":
            raise XeroAuthError("Xero no longer accepts this connection (it expired after 60 days without use, or "
                                f"was revoked), so it needs to be connected again. {RECONNECT}",
                                r.status_code, r.text[:500], code=code)
        if code in ("invalid_client", "unauthorized_client"):
            raise XeroError("Xero didn't accept ShipMatch's app keys. Check XERO_CLIENT_ID and XERO_CLIENT_SECRET "
                            "in the .env file against the app at developer.xero.com.", r.status_code, r.text[:500], code)
        raise XeroError(f"Xero sign-in failed ({r.status_code} {code})".strip(), r.status_code, r.text[:500], code)
    return payload


def store_tokens(conn: XeroConnection, tok: dict, save: bool = True) -> XeroConnection:
    now = timezone.now()
    conn.access_token = tok["access_token"]
    conn.refresh_token = tok.get("refresh_token") or conn.refresh_token   # rotates: always keep the newest
    conn.access_expires_at = now + timedelta(seconds=int(tok.get("expires_in") or ACCESS_SECONDS))
    conn.refresh_expires_at = now + timedelta(days=REFRESH_DAYS)
    conn.needs_reconnect, conn.last_error = False, ""
    if save:
        conn.save()
    return conn


def exchange_code(code: str, verifier: str | None = None, http: httpx.Client | None = None) -> dict:
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": settings.XERO_REDIRECT_URI}
    if verifier:
        data["code_verifier"] = verifier
    return _token_request(data, http)


def refresh(conn: XeroConnection, http: httpx.Client | None = None) -> XeroConnection:
    """Refresh once across all workers: the row lock stops two processes from spending the same refresh token
    (Xero rotates it, so the slower one would otherwise be locked out)."""
    seen = conn.access_token
    try:
        with transaction.atomic():
            locked = XeroConnection.objects.select_for_update().get(pk=conn.pk)
            if locked.access_valid and locked.access_token != seen:
                fresh = locked   # another worker refreshed while we waited for the lock
            elif not locked.refresh_token:
                raise XeroAuthError(f"There is no saved Xero sign-in. {RECONNECT}")
            else:
                fresh = store_tokens(locked, _token_request(
                    {"grant_type": "refresh_token", "refresh_token": locked.refresh_token}, http))
    except XeroAuthError as e:   # saved outside the rolled-back transaction
        mark_needs_reconnect(conn, str(e))
        raise
    for name in ("access_token", "refresh_token", "access_expires_at", "refresh_expires_at", "needs_reconnect"):
        setattr(conn, name, getattr(fresh, name))
    return conn


def mark_needs_reconnect(conn: XeroConnection, error: str) -> None:
    from apps.core.utils import audit

    already = XeroConnection.objects.filter(pk=conn.pk, needs_reconnect=True).exists()
    XeroConnection.objects.filter(pk=conn.pk).update(needs_reconnect=True, last_error=error[:1000])
    conn.needs_reconnect, conn.last_error = True, error
    if not already:
        audit(conn.organization, "xero.needs_reconnect", conn, error=error[:300], company=conn.tenant_name)


def revoke(conn: XeroConnection, http: httpx.Client | None = None) -> bool:
    """Tell Xero to cancel the sign-in (on disconnect). Best effort."""
    if _blocked() or not conn.refresh_token:
        return False
    body = {"token": conn.refresh_token}
    auth = (settings.XERO_CLIENT_ID, settings.XERO_CLIENT_SECRET) if settings.XERO_CLIENT_SECRET else None
    if auth is None:
        body["client_id"] = settings.XERO_CLIENT_ID
    client = http or httpx.Client(timeout=20)
    try:
        r = client.post(REVOKE_URL, data=body, auth=auth, headers={"Accept": "application/json"})
        return r.status_code == 200
    except httpx.HTTPError:
        return False
    finally:
        if http is None:
            client.close()


def auth_event_id(access_token: str) -> str:
    """The sign-in event in the access token (a JWT), used to list only the organisations just connected.
    Read without verifying: it only narrows a list Xero itself returns."""
    try:
        payload = access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(claims.get("authentication_event_id") or "")
    except (IndexError, ValueError, TypeError):
        return ""


def connections(access_token: str, http: httpx.Client | None = None, event_id: str = "") -> list[dict]:
    """Xero organisations this sign-in reaches: [{id, tenantId, tenantName, tenantType}]."""
    if _blocked():
        raise XeroError(DEMO_BLOCKED)
    client = http or httpx.Client(timeout=30)
    try:
        r = client.get(CONNECTIONS_URL, params={"authEventId": event_id} if event_id else None,
                       headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
    except httpx.HTTPError as e:
        raise XeroError(f"Couldn't reach Xero ({type(e).__name__}). Try again in a few minutes.") from e
    finally:
        if http is None:
            client.close()
    if r.status_code >= 400:
        raise _error(r)
    rows = r.json() if r.content else []
    return [{"id": str(c.get("id") or ""), "tenantId": str(c.get("tenantId") or ""),
             "tenantName": str(c.get("tenantName") or "")[:200]}
            for c in rows if isinstance(c, dict) and (c.get("tenantType") or "ORGANISATION") == "ORGANISATION"
            and c.get("tenantId")]


# --------------------------------------------------------------------------- values


def parse_date(value) -> date | None:
    """Xero dates: '/Date(1539993600000+0000)/' in JSON, '2026-10-03T00:00:00' in *String fields."""
    if not value:
        return None
    text = str(value)
    m = re.match(r"/Date\((-?\d+)([+-]\d{4})?\)/", text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=dt_timezone.utc).date()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def money(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def safe_filename(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", (name or "document.pdf")).strip(" .") or "document"
    if not stem.lower().endswith(".pdf"):
        stem += ".pdf"
    return stem[-120:]


def _where_text(value: str) -> str | None:
    """A value safe to put between double quotes in a Xero where filter, or None if it isn't."""
    return None if any(c in value for c in '"\\') else value


# --------------------------------------------------------------------------- API client


class XeroClient:
    def __init__(self, conn: XeroConnection, http: httpx.Client | None = None, sleep=time.sleep):
        self.conn = conn
        self.http = http or httpx.Client(timeout=60)
        self._own_http = http is None
        self.sleep = sleep
        self.day_remaining: int | None = None
        self.minute_remaining: int | None = None

    def close(self) -> None:
        if self._own_http:
            self.http.close()

    def _note_limits(self, r: httpx.Response) -> None:
        for header, attr in (("x-daylimit-remaining", "day_remaining"), ("x-minlimit-remaining", "minute_remaining")):
            try:
                setattr(self, attr, int(r.headers[header]))
            except (KeyError, ValueError):
                pass

    def _backoff(self, attempt: int) -> None:
        self.sleep(min(20.0, 2 ** attempt + random.random()))

    def request(self, method: str, path: str, *, params=None, json_body=None, content: bytes | None = None,
                content_type: str = "", idempotency_key: str = "") -> dict:
        if _blocked():
            raise XeroError(DEMO_BLOCKED)
        if self.conn.needs_reconnect:
            raise XeroAuthError(f"Xero needs to be connected again. {RECONNECT}")
        if not self.conn.tenant_id:
            raise XeroError("Choose which Xero organisation to use in Settings > Accounting.")
        if not self.conn.access_valid:
            refresh(self.conn, self.http)
        refreshed = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = {"Authorization": f"Bearer {self.conn.access_token}", "Xero-tenant-id": self.conn.tenant_id,
                       "Accept": "application/json"}
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key[:128]
            if content_type:
                headers["Content-Type"] = content_type
            try:
                r = self.http.request(method, f"{API_BASE}/{path}", params=params, json=json_body, content=content,
                                      headers=headers)
            except httpx.HTTPError as e:
                if attempt == MAX_ATTEMPTS:
                    raise XeroError(f"Could not reach Xero ({type(e).__name__}). Try again in a few minutes.") from e
                self._backoff(attempt)
                continue
            self._note_limits(r)
            if r.status_code == 401 and not refreshed:
                refresh(self.conn, self.http)   # a token that looked valid was refused: refresh once
                refreshed = True
                continue
            if r.status_code == 429:
                problem = (r.headers.get("x-rate-limit-problem") or "").lower()
                try:
                    wait = float(r.headers.get("retry-after") or 60)
                except ValueError:
                    wait = 60.0
                if problem == "day" or wait > MAX_WAIT_SECONDS:
                    raise XeroDailyLimit(_limit_message(problem, wait), wait, problem)
                if attempt == MAX_ATTEMPTS:
                    raise XeroRateLimited(_limit_message(problem, wait), wait, problem)
                log.info("Xero %s limit: waiting %.0f s", problem or "rate", wait)
                self.sleep(wait)
                continue
            if r.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                self._backoff(attempt)
                continue
            if r.status_code >= 400:
                err = _error(r)
                log.warning("Xero %s %s failed status=%s: %s", method, path, r.status_code, err)
                if isinstance(err, XeroAuthError):
                    mark_needs_reconnect(self.conn, str(err))
                raise err
            return r.json() if r.content else {}
        raise XeroError("Xero did not respond after several attempts. Try again in a few minutes.")

    # ---- organisation

    def organisation(self) -> dict:
        return (self.request("GET", "Organisation").get("Organisations") or [{}])[0]

    def currencies(self) -> list[str]:
        return [str(c.get("Code") or "").upper() for c in self.request("GET", "Currencies").get("Currencies") or []
                if c.get("Code")]

    def sync_organisation(self) -> XeroConnection:
        """Store the organisation's name, base currency and the currencies added in Xero."""
        info = self.organisation()
        self.conn.tenant_name = str(info.get("Name") or self.conn.tenant_name)[:200]
        self.conn.home_currency = str(info.get("BaseCurrency") or "")[:3].upper()
        self.conn.short_code = str(info.get("ShortCode") or "")[:20]
        try:
            self.conn.currencies = self.currencies()
        except XeroAuthError:
            raise
        except XeroError as e:   # without accounting.settings the base currency alone is still known
            log.warning("Could not read Xero currencies: %s", e)
        self.conn.save(update_fields=["tenant_name", "home_currency", "short_code", "currencies"])
        return self.conn

    # ---- contacts and accounts

    def get_contact(self, contact_id: str) -> dict | None:
        try:
            return (self.request("GET", f"Contacts/{contact_id}").get("Contacts") or [None])[0]
        except XeroNotFound:
            return None

    def find_contact(self, name: str) -> dict | None:
        """The contact with this exact name (any case), active first, archived ones included."""
        wanted = name.strip().lower()
        safe = _where_text(name.strip())
        if safe is not None:
            params = {"where": f'Name=="{safe}"', "includeArchived": "true"}
        else:
            params = {"searchTerm": name.strip()[:100], "includeArchived": "true", "page": 1}
        rows = self.request("GET", "Contacts", params=params).get("Contacts") or []
        exact = [c for c in rows if str(c.get("Name") or "").strip().lower() == wanted]
        exact.sort(key=lambda c: c.get("ContactStatus") not in (None, "ACTIVE"))
        return exact[0] if exact else None

    def create_contact(self, name: str, currency: str | None = None, key: str = "") -> dict:
        body = {"Name": name[:255]}
        if currency:
            body["DefaultCurrency"] = currency
        data = self.request("PUT", "Contacts", json_body={"Contacts": [body]}, idempotency_key=key)
        return (data.get("Contacts") or [{}])[0]

    def expense_accounts(self) -> list[dict]:
        rows = self.request("GET", "Accounts", params={"where": 'Class=="EXPENSE" AND Status=="ACTIVE"'}).get("Accounts") or []
        out = [{"id": str(a["Code"]), "name": str(a.get("Name") or a["Code"]),
                "type": EXPENSE_TYPES.get(str(a.get("Type") or ""), str(a.get("Type") or "").title())}
               for a in rows if a.get("Code")]
        return sorted(out, key=lambda a: a["name"].lower())

    # ---- bills and credit notes

    def create_invoice(self, payload: dict, idempotency_key: str) -> dict:
        data = self.request("PUT", "Invoices", json_body={"Invoices": [payload]}, idempotency_key=idempotency_key)
        return _first_or_raise(data, "Invoices")

    def create_credit_note(self, payload: dict, idempotency_key: str) -> dict:
        data = self.request("PUT", "CreditNotes", json_body={"CreditNotes": [payload]}, idempotency_key=idempotency_key)
        return _first_or_raise(data, "CreditNotes")

    def find_bill(self, contact_id: str, number: str) -> dict | None:
        """A live ACCPAY bill for this contact with this number (used before creating one again after a long gap,
        when the Idempotency-Key may have expired)."""
        if not number or not _GUID.match(contact_id or "") or "," in number:
            return None
        rows = self.request("GET", "Invoices", params={
            "where": 'Type=="ACCPAY"', "ContactIDs": contact_id, "InvoiceNumbers": number,
            "Statuses": "DRAFT,SUBMITTED,AUTHORISED,PAID", "page": 1}).get("Invoices") or []
        return rows[0] if rows else None

    def find_credit_note(self, contact_id: str, number: str) -> dict | None:
        safe = _where_text(number or "")
        if not safe or not _GUID.match(contact_id or ""):
            return None
        where = (f'Type=="ACCPAYCREDIT" AND CreditNoteNumber=="{safe}" AND Contact.ContactID==Guid("{contact_id}") '
                 'AND Status!="DELETED" AND Status!="VOIDED"')
        rows = self.request("GET", "CreditNotes", params={"where": where, "page": 1}).get("CreditNotes") or []
        return rows[0] if rows else None

    def attach(self, endpoint: str, entity_id: str, filename: str, content: bytes) -> str:
        """Attach the PDF to an invoice (endpoint "Invoices") or credit note ("CreditNotes")."""
        name = safe_filename(filename)
        data = self.request("PUT", f"{endpoint}/{entity_id}/Attachments/{quote(name)}", content=content,
                            content_type="application/pdf", idempotency_key=f"att-{entity_id}-{name}")
        attachment = (data.get("Attachments") or [{}])[0]
        if not attachment.get("AttachmentID"):
            raise XeroError("Xero didn't confirm the attachment. Post again to retry.")
        return str(attachment["AttachmentID"])

    def add_note(self, endpoint: str, entity_id: str, text: str) -> None:
        """A line in the bill's history and notes in Xero (where it came from). Best effort: a failure here must
        never lose the ID of the bill just created (the next call reports a lasting problem anyway)."""
        try:
            self.request("PUT", f"{endpoint}/{entity_id}/History",
                         json_body={"HistoryRecords": [{"Details": text[:450]}]})
        except XeroError as e:
            log.info("Could not add a history note in Xero: %s", e)

    # ---- payment status

    def invoices_by_ids(self, ids: list[str]) -> list[dict]:
        out = []
        ids = [i for i in ids if _GUID.match(i or "")]
        for start in range(0, len(ids), IDS_PER_CALL):
            chunk = ids[start:start + IDS_PER_CALL]
            out += self.request("GET", "Invoices", params={
                "IDs": ",".join(chunk), "Statuses": "DRAFT,SUBMITTED,AUTHORISED,PAID,VOIDED,DELETED",
                "page": 1}).get("Invoices") or []
        return out

    def credit_notes_by_ids(self, ids: list[str]) -> list[dict]:
        out = []
        ids = [i for i in ids if _GUID.match(i or "")]
        for start in range(0, len(ids), CREDIT_IDS_PER_CALL):
            chunk = ids[start:start + CREDIT_IDS_PER_CALL]
            where = " OR ".join(f'CreditNoteID==Guid("{i}")' for i in chunk)
            out += self.request("GET", "CreditNotes", params={"where": where, "page": 1}).get("CreditNotes") or []
        return out


def _first_or_raise(data: dict, key: str) -> dict:
    item = (data.get(key) or [{}])[0]
    found = [str(v.get("Message") or "") for v in item.get("ValidationErrors") or []]
    if item.get("HasErrors") or found or item.get("StatusAttributeString") == "ERROR":
        raise XeroValidationError(found)
    return item


def _limit_message(problem: str, wait: float) -> str:
    if problem == "day" or wait > MAX_WAIT_SECONDS:
        when = timezone.localtime(timezone.now() + timedelta(seconds=wait))
        return (f"Xero's daily limit of API calls for this organisation is used up. ShipMatch tries again after "
                f"{when:%H:%M} ({when:%d %b}); nothing was lost.")
    if problem == "concurrent":
        return "Xero is busy with other requests from ShipMatch for this organisation. Try again in a minute."
    return "Xero asked ShipMatch to slow down (too many calls in a minute). Try again in a minute."
