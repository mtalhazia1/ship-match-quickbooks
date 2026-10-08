"""The accounting system an organization posts to: QuickBooks Online or Xero, behind one interface.

An organization has one active posting target. `active_connection(org)` returns its QBOConnection or
XeroConnection (a Xero sign-in still waiting for the admin to pick an organisation doesn't count), and
`provider_for(org)` wraps it with the client calls that posting and payment checks need:

    ensure_company()                      read company settings once (home currency, multicurrency)
    check_currency(doc)                   PostingBlocked when the system can't take the document's currency
    resolve_vendor(doc)                   (VendorMapping, vendor or contact id), found or created
    account_for(mapping)                  the vendor's expense account, else the default, else PostingBlocked
    create(doc, shipment, vendor, account, pb) -> Created(id, number, raw)   bill or vendor credit
    attach(pb, doc)                       attach the PDF, return the attachment id
    payment_statuses(bills)               {PostedBill.pk: PaymentInfo}, batched
    expense_accounts()                    [{id, name, type}] for the settings page
    explain(e)                            an error in plain words
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.conf import settings

from apps.accounting.models import PostedBill, QBOConnection, VendorMapping, XeroConnection

from . import posting

log = logging.getLogger(__name__)


@dataclass
class Created:
    id: str
    number: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class PaymentInfo:
    """What the accounting system says about one posted bill or vendor credit."""

    status: str                      # PostedBill.Payment value
    total: Decimal | None = None
    paid: Decimal | None = None      # paid, or for a credit: used against bills
    due: Decimal | None = None       # still to pay, or for a credit: not used yet
    currency: str = ""
    due_date: date | None = None
    paid_on: date | None = None
    payments: list = field(default_factory=list)   # [{date, amount, kind, reference}]


def active_connection(org) -> QBOConnection | XeroConnection | None:
    """The connection bills post to. If both exist (a switch in progress), the newer one wins."""
    if org is None:
        return None
    candidates = [c for c in (QBOConnection.objects.filter(organization=org).first(),
                              XeroConnection.objects.filter(organization=org).exclude(tenant_id="").first()) if c]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.connected_at)


def provider_for(org, client=None):
    """A provider for the organization's active system, or for the given client (tests, management commands)."""
    from .quickbooks import QBOClient
    from .xero import XeroClient

    if isinstance(client, QBOClient):
        return QuickBooksProvider(client.conn, client)
    if isinstance(client, XeroClient):
        return XeroProvider(client.conn, client)
    conn = active_connection(org)
    if isinstance(conn, QBOConnection):
        return QuickBooksProvider(conn)
    if isinstance(conn, XeroConnection):
        return XeroProvider(conn)
    return None


def system_name(org) -> str:
    conn = active_connection(org)
    return conn.system_name if conn else ""


# --------------------------------------------------------------------------- QuickBooks


class QuickBooksProvider:
    key, name = "quickbooks", "QuickBooks"

    def __init__(self, conn: QBOConnection, client=None):
        from .quickbooks import QBOAuthError, QBOClient, QBOError

        self.conn = conn
        self.client = client or QBOClient(conn)
        self.errors, self.stop_errors = (QBOError,), (QBOAuthError,)

    @property
    def ledger_id(self) -> str:
        return self.conn.realm_id

    def ensure_company(self) -> None:
        if not self.conn.home_currency:
            try:
                self.client.sync_company()
            except self.errors as e:
                log.warning("Could not read QuickBooks company settings: %s", e)

    def check_currency(self, doc) -> None:
        posting._check_currency(doc, self.conn)

    def resolve_vendor(self, doc) -> tuple[VendorMapping, str]:
        mapping = posting.resolve_vendor(self.client, doc, self.conn)
        return mapping, mapping.qbo_vendor_id

    def account_for(self, mapping: VendorMapping) -> str:
        account = mapping.expense_account_id or self.conn.default_expense_account_id
        if not account:
            raise posting.PostingBlocked(f"No expense account for {mapping.display_name}. Set a default in Settings > "
                                         "Accounting, or a rule on the invoice.")
        return account

    def create(self, doc, shipment, vendor_id: str, account: str, pb: PostedBill) -> Created:
        if doc.is_credit:
            made = self.client.create_vendor_credit(
                posting.build_vendor_credit_payload(doc, shipment, vendor_id, account, self.conn), pb.request_id)
        else:
            made = self.client.create_bill(
                posting.build_bill_payload(doc, shipment, vendor_id, account, self.conn), pb.request_id)
        return Created(str(made["Id"]), str(made.get("DocNumber") or ""), made)

    def attach(self, pb: PostedBill, doc) -> str:
        with doc.file.open("rb") as fh:
            return self.client.upload_attachment(pb.qbo_bill_id, doc.pdf_filename, fh.read(),
                                                 entity_type="VendorCredit" if doc.is_credit else "Bill")

    def audit_ref(self, pb: PostedBill) -> str:
        return pb.qbo_bill_id

    def explain(self, e: Exception) -> str:
        return posting._explain(e)

    def expense_accounts(self) -> list[dict]:
        return self.client.expense_accounts()

    def payment_statuses(self, bills: list[PostedBill]) -> dict[int, PaymentInfo]:
        from .payment_reading import quickbooks_statuses

        return quickbooks_statuses(self.client, bills)


# --------------------------------------------------------------------------- Xero


class XeroProvider:
    key, name = "xero", "Xero"

    def __init__(self, conn: XeroConnection, client=None):
        from .xero import XeroAuthError, XeroClient, XeroDailyLimit, XeroError

        self.conn = conn
        self.client = client or XeroClient(conn)
        # A rejected sign-in or a used-up daily allowance stops the run: every other call would fail the same way.
        self.errors, self.stop_errors = (XeroError,), (XeroAuthError, XeroDailyLimit)

    @property
    def ledger_id(self) -> str:
        return self.conn.tenant_id

    def ensure_company(self) -> None:
        if not self.conn.home_currency:
            try:
                self.client.sync_organisation()
            except self.errors as e:
                log.warning("Could not read the Xero organisation's settings: %s", e)

    def check_currency(self, doc) -> None:
        from .xero_posting import check_currency

        check_currency(doc, self.conn)

    def resolve_vendor(self, doc) -> tuple[VendorMapping, str]:
        from .xero_posting import resolve_contact

        mapping = resolve_contact(self.client, doc, self.conn)
        return mapping, mapping.xero_contact_id

    def account_for(self, mapping: VendorMapping) -> str:
        account = mapping.xero_account_code or self.conn.default_account_code
        if not account:
            raise posting.PostingBlocked(f"No expense account for {mapping.display_name}. Set a default in Settings > "
                                         "Accounting, or a rule on the invoice.")
        return account

    def create(self, doc, shipment, vendor_id: str, account: str, pb: PostedBill) -> Created:
        from .xero_posting import create_document

        return create_document(self.client, doc, shipment, vendor_id, account, self.conn, pb)

    def attach(self, pb: PostedBill, doc) -> str:
        with doc.file.open("rb") as fh:
            content = fh.read()
        return self.client.attach("CreditNotes" if doc.is_credit else "Invoices", pb.qbo_bill_id,
                                  doc.pdf_filename, content)

    def audit_ref(self, pb: PostedBill) -> str:
        return pb.display_number

    def explain(self, e: Exception) -> str:
        return str(e)

    def expense_accounts(self) -> list[dict]:
        return self.client.expense_accounts()

    def payment_statuses(self, bills: list[PostedBill]) -> dict[int, PaymentInfo]:
        from .payment_reading import xero_statuses

        return xero_statuses(self.client, bills)


def bill_link(pb: PostedBill) -> str:
    """Where the bill opens in the accounting system, when it can be linked to."""
    if not pb.qbo_bill_id:
        return ""
    if pb.system == PostedBill.System.QUICKBOOKS:
        host = "app.sandbox.qbo.intuit.com" if settings.QBO_ENVIRONMENT == "sandbox" else "app.qbo.intuit.com"
        page = "vendorcredit" if pb.is_credit else "bill"
        return f"https://{host}/app/{page}?txnId={pb.qbo_bill_id}"
    conn = XeroConnection.objects.filter(organization_id=pb.organization_id).only("short_code", "tenant_id").first()
    if pb.system == PostedBill.System.XERO and conn and conn.short_code and conn.tenant_id == pb.ledger_id:
        target = (f"/AP/ViewCreditNote.aspx?creditNoteID={pb.qbo_bill_id}" if pb.is_credit
                  else f"/AccountsPayable/View.aspx?InvoiceID={pb.qbo_bill_id}")
        return f"https://go.xero.com/organisationlogin/default.aspx?shortcode={conn.short_code}&redirecturl={target}"
    return ""


def set_rule_account(mapping: VendorMapping, account_id: str, account_name: str) -> tuple[str, str]:
    """Save a vendor's expense account for the organization's active system (QuickBooks account Id or Xero
    account code). Returns the (id, name) saved."""
    account_id, account_name = (account_id or "").strip(), (account_name or "").strip()
    conn = active_connection(mapping.organization)
    if isinstance(conn, XeroConnection):
        mapping.xero_account_code, mapping.xero_account_name = account_id[:20], account_name[:200]
        return mapping.xero_account_code, mapping.xero_account_name
    mapping.expense_account_id, mapping.expense_account_name = account_id[:40], account_name[:200]
    return mapping.expense_account_id, mapping.expense_account_name


def rule_account(mapping: VendorMapping | None, system: str) -> tuple[str, str]:
    """(id, name) of a vendor's expense account rule in one system."""
    if mapping is None:
        return "", ""
    if system == "xero":
        return mapping.xero_account_code, mapping.xero_account_name
    return mapping.expense_account_id, mapping.expense_account_name
