"""QuickBooks Online API client: OAuth 2.0, company settings, vendors, accounts, bills, vendor credits and
attachments.

Docs: https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/bill
Checked against Intuit's rules as of Oct 2026:
  * minorversion 75 or later is required (1-74 were retired on 1 Aug 2025);
  * access tokens last 60 minutes; refresh tokens rotate (always store the newest one) and now have
    a maximum life of five years, after which the customer must reconnect;
  * 500 requests per minute and 10 concurrent requests per company; 429 means back off.
All bill and vendor credit writes carry a `requestid`: sending the same requestid twice returns the first result
instead of creating a second bill, so retries can never double-post.
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounting.models import QBOConnection

log = logging.getLogger(__name__)

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
API_BASE = {"sandbox": "https://sandbox-quickbooks.api.intuit.com", "production": "https://quickbooks.api.intuit.com"}
SCOPE = "com.intuit.quickbooks.accounting"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


class QBOError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = "", code: str = "", intuit_tid: str = ""):
        super().__init__(message)
        self.status, self.body, self.code, self.intuit_tid = status, body, code, intuit_tid


class QBOAuthError(QBOError):
    """The refresh token was rejected (expired, revoked or replaced). The customer must reconnect."""


# --------------------------------------------------------------------------- OAuth


def authorize_url(state: str) -> str:
    return f"{AUTH_URL}?" + urlencode({
        "client_id": settings.QBO_CLIENT_ID, "response_type": "code", "scope": SCOPE,
        "redirect_uri": settings.QBO_REDIRECT_URI, "state": state,
    })


def _token_request(data: dict, http: httpx.Client | None = None) -> dict:
    client = http or httpx.Client(timeout=30)
    try:
        r = client.post(TOKEN_URL, data=data, auth=(settings.QBO_CLIENT_ID, settings.QBO_CLIENT_SECRET),
                        headers={"Accept": "application/json"})
    finally:
        if http is None:
            client.close()
    if r.status_code >= 400:
        try:
            err = r.json().get("error", "")
        except ValueError:
            err = ""
        if err == "invalid_grant":
            raise QBOAuthError("QuickBooks no longer accepts this connection, so it needs to be connected again. "
                               "An admin can reconnect in Settings > Accounting.",
                               r.status_code, r.text[:500], code=err)
        raise QBOError(f"Token request failed ({r.status_code} {err})".strip(), r.status_code, r.text[:500], code=err)
    return r.json()


def _store_tokens(conn: QBOConnection, tok: dict) -> QBOConnection:
    now = timezone.now()
    conn.access_token = tok["access_token"]
    conn.refresh_token = tok.get("refresh_token", conn.refresh_token)
    conn.access_expires_at = now + timedelta(seconds=int(tok.get("expires_in", 3600)))
    if tok.get("x_refresh_token_expires_in"):
        conn.refresh_expires_at = now + timedelta(seconds=int(tok["x_refresh_token_expires_in"]))
    conn.needs_reconnect, conn.last_error = False, ""
    conn.save()
    return conn


def exchange_code(org, code: str, realm_id: str, http: httpx.Client | None = None) -> QBOConnection:
    tok = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": settings.QBO_REDIRECT_URI}, http)
    conn = QBOConnection.objects.filter(organization=org).first() or QBOConnection(organization=org)
    if conn.pk and conn.realm_id and conn.realm_id != realm_id:
        # A different QuickBooks company: IDs of vendors and accounts from the old one mean nothing here.
        from apps.accounting.models import VendorMapping

        VendorMapping.objects.filter(organization=org).update(qbo_vendor_id="", expense_account_id="")
        conn.default_expense_account_id, conn.company_name, conn.home_currency = "", "", ""
    conn.realm_id = realm_id
    return _store_tokens(conn, tok)


def refresh(conn: QBOConnection, http: httpx.Client | None = None) -> QBOConnection:
    """Refresh once across all workers: the row lock stops two processes from using the same refresh
    token at the same time (Intuit rotates it, so the slower one would otherwise fail)."""
    try:
        with transaction.atomic():
            locked = QBOConnection.objects.select_for_update().get(pk=conn.pk)
            if locked.access_valid and locked.access_token != conn.access_token:
                fresh = locked  # another worker refreshed while we waited
            else:
                fresh = _store_tokens(locked, _token_request(
                    {"grant_type": "refresh_token", "refresh_token": locked.refresh_token}, http))
    except QBOAuthError as e:  # saved outside the rolled-back transaction
        QBOConnection.objects.filter(pk=conn.pk).update(needs_reconnect=True, last_error=str(e))
        if not conn.needs_reconnect:
            from apps.core.utils import audit

            audit(conn.organization, "qbo.needs_reconnect", conn, error=str(e)[:300])
        conn.needs_reconnect = True
        raise
    for field in ("access_token", "refresh_token", "access_expires_at", "refresh_expires_at", "needs_reconnect"):
        setattr(conn, field, getattr(fresh, field))
    return conn


def revoke(conn: QBOConnection, http: httpx.Client | None = None) -> bool:
    """Tell Intuit to invalidate the tokens (used on disconnect). Best effort."""
    client = http or httpx.Client(timeout=20)
    try:
        r = client.post(REVOKE_URL, json={"token": conn.refresh_token},
                        auth=(settings.QBO_CLIENT_ID, settings.QBO_CLIENT_SECRET), headers={"Accept": "application/json"})
        return r.status_code == 200
    except httpx.HTTPError:
        return False
    finally:
        if http is None:
            client.close()


# --------------------------------------------------------------------------- API client


def _fault(r: httpx.Response) -> tuple[str, str]:
    """Readable message and code from QuickBooks' Fault JSON."""
    try:
        data = r.json()
    except ValueError:
        return r.text[:300], ""
    errors = (data.get("Fault") or data.get("fault") or {}).get("Error") or (data.get("Fault") or {}).get("error") or []
    if errors:
        e = errors[0]
        message = e.get("Message") or e.get("message") or ""
        detail = e.get("Detail") or e.get("detail") or ""
        return (f"{message}: {detail}" if detail and detail != message else message), str(e.get("code", ""))
    return json.dumps(data)[:300], ""


class QBOClient:
    def __init__(self, conn: QBOConnection, http: httpx.Client | None = None):
        self.conn = conn
        self.http = http or httpx.Client(timeout=60)
        self.base = f"{API_BASE[settings.QBO_ENVIRONMENT]}/v3/company/{conn.realm_id}"

    def _request(self, method: str, path: str, *, params=None, json_body=None, files=None) -> dict:
        if self.conn.needs_reconnect:
            raise QBOAuthError("QuickBooks needs to be connected again. An admin can reconnect in Settings > Accounting.")
        if not self.conn.access_valid:
            refresh(self.conn, self.http)
        params = {"minorversion": settings.QBO_MINOR_VERSION, **(params or {})}
        refreshed = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = {"Authorization": f"Bearer {self.conn.access_token}", "Accept": "application/json"}
            try:
                r = self.http.request(method, f"{self.base}/{path}", params=params, json=json_body, files=files,
                                      headers=headers)
            except httpx.HTTPError as e:
                if attempt == MAX_ATTEMPTS:
                    raise QBOError(f"Could not reach QuickBooks: {e}") from e
                self._backoff(attempt, None)
                continue
            tid = r.headers.get("intuit_tid", "")
            if r.status_code == 401 and not refreshed:
                refresh(self.conn, self.http)
                refreshed = True
                continue
            if r.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                self._backoff(attempt, r.headers.get("retry-after"))
                continue
            if r.status_code >= 400:
                message, code = _fault(r)
                log.warning("QuickBooks %s %s failed status=%s code=%s intuit_tid=%s: %s",
                            method, path, r.status_code, code, tid, message)
                raise QBOError(f"QuickBooks rejected the request: {message}", r.status_code, r.text[:1000],
                               code=code, intuit_tid=tid)
            return r.json()
        raise QBOError("QuickBooks did not respond after several attempts")

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> None:
        try:
            wait = float(retry_after) if retry_after else 0.0
        except ValueError:
            wait = 0.0
        time.sleep(max(wait, min(20.0, 2 ** attempt + random.random())))

    @staticmethod
    def _quote(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    def query(self, sql: str) -> dict:
        return self._request("GET", "query", params={"query": sql}).get("QueryResponse", {})

    # ---- company

    def company_info(self) -> dict:
        return self._request("GET", f"companyinfo/{self.conn.realm_id}").get("CompanyInfo", {})

    def preferences(self) -> dict:
        prefs = self._request("GET", "preferences").get("Preferences", {})
        cur = prefs.get("CurrencyPrefs", {})
        return {"multicurrency": bool(cur.get("MultiCurrencyEnabled")),
                "home_currency": (cur.get("HomeCurrency") or {}).get("value", "")}

    def sync_company(self) -> QBOConnection:
        """Store the company name and currency settings on the connection."""
        info, prefs = self.company_info(), self.preferences()
        self.conn.company_name = (info.get("CompanyName") or "")[:200]
        self.conn.home_currency = prefs["home_currency"][:3]
        self.conn.multicurrency = prefs["multicurrency"]
        self.conn.save(update_fields=["company_name", "home_currency", "multicurrency"])
        return self.conn

    # ---- vendors and accounts

    def find_vendor(self, display_name: str) -> dict | None:
        """The vendor with this exact display name, active or not."""
        rows = self.query(f"select Id, DisplayName, Active, CurrencyRef from Vendor "
                          f"where DisplayName = '{self._quote(display_name[:100])}' and Active in (true, false)")
        vendors = rows.get("Vendor") or []
        return vendors[0] if vendors else None

    def create_vendor(self, display_name: str, currency: str | None = None) -> dict:
        body = {"DisplayName": display_name[:100]}
        if currency:
            body["CurrencyRef"] = {"value": currency}
        return self._request("POST", "vendor", json_body=body)["Vendor"]

    def expense_accounts(self) -> list[dict]:
        out = []
        for account_type in ("Expense", "Cost of Goods Sold", "Other Expense"):
            rows = self.query(f"select Id, Name, AccountType from Account where AccountType = '{account_type}' and Active = true")
            out += [{"id": a["Id"], "name": a["Name"], "type": a["AccountType"]} for a in rows.get("Account", [])]
        return sorted(out, key=lambda a: a["name"])

    # ---- bills

    def create_bill(self, payload: dict, request_id: str) -> dict:
        return self._request("POST", "bill", params={"requestid": request_id}, json_body=payload)["Bill"]

    def get_bill(self, bill_id: str) -> dict:
        return self._request("GET", f"bill/{bill_id}").get("Bill", {})

    # ---- vendor credits (credit notes)

    def create_vendor_credit(self, payload: dict, request_id: str) -> dict:
        """A VendorCredit lowers what is owed to a vendor. Same requestid rule as bills: a retry with the same
        id returns the first credit instead of creating a second one.
        https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/vendorcredit"""
        return self._request("POST", "vendorcredit", params={"requestid": request_id}, json_body=payload)["VendorCredit"]

    def get_vendor_credit(self, credit_id: str) -> dict:
        return self._request("GET", f"vendorcredit/{credit_id}").get("VendorCredit", {})

    def upload_attachment(self, bill_id: str, filename: str, content: bytes, entity_type: str = "Bill") -> str:
        """Attach the PDF to a Bill or a VendorCredit (`entity_type`)."""
        metadata = {
            "AttachableRef": [{"EntityRef": {"type": entity_type, "value": bill_id}}],
            "FileName": filename, "ContentType": "application/pdf",
        }
        files = {
            "file_metadata_01": (None, json.dumps(metadata), "application/json"),
            "file_content_01": (filename, content, "application/pdf"),
        }
        resp = self._request("POST", "upload", files=files)
        item = (resp.get("AttachableResponse") or [{}])[0]
        if "Fault" in item:
            raise QBOError(f"QuickBooks rejected the attachment: {item['Fault']}")
        return item["Attachable"]["Id"]

    # ---- payment status (read in batches: one query per 100 IDs)

    def _by_ids(self, entity: str, ids: list[str], batch: int = 100) -> list[dict]:
        ids = [i for i in dict.fromkeys(str(i) for i in ids) if i.isdigit()]
        out = []
        for start in range(0, len(ids), batch):
            chunk = ", ".join(f"'{i}'" for i in ids[start:start + batch])
            out += self.query(f"select * from {entity} where Id in ({chunk}) maxresults 1000").get(entity) or []
        return out

    def bills_by_ids(self, ids: list[str]) -> list[dict]:
        return self._by_ids("Bill", ids)

    def bill_payments_by_ids(self, ids: list[str]) -> list[dict]:
        return self._by_ids("BillPayment", ids)

    def vendor_credits_by_ids(self, ids: list[str]) -> list[dict]:
        return self._by_ids("VendorCredit", ids)

    def exists(self, entity: str, entity_id: str) -> bool | None:
        """True if it can be read, False if QuickBooks says it doesn't exist (deleted), None if unsure."""
        try:
            self._request("GET", f"{entity.lower()}/{entity_id}")
            return True
        except QBOAuthError:
            raise
        except QBOError as e:
            if e.status == 404 or e.code == "610" or "not found" in str(e).lower():
                return False
            return None
