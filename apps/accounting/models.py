from __future__ import annotations

import re

from django.db import models
from django.utils import timezone

from apps.core.crypto import EncryptedTextField
from apps.core.models import Organization
from apps.documents.models import Document
from apps.shipments.models import Shipment

_SUFFIXES = re.compile(r"\b(co|company|ltd|limited|llc|inc|corp|corporation|gmbh|sa|as|bv|plc|pte|pvt)\b\.?")


def vendor_key(name: str) -> str:
    """Normalize a vendor name so 'Harborlink Logistics, LLC' == 'HARBORLINK LOGISTICS LLC'."""
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


class QBOConnection(models.Model):
    """OAuth tokens for one organization's QuickBooks Online company."""

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="qbo")
    realm_id = models.CharField(max_length=40)
    access_token = EncryptedTextField()
    refresh_token = EncryptedTextField()
    access_expires_at = models.DateTimeField()
    refresh_expires_at = models.DateTimeField(null=True, blank=True)
    default_expense_account_id = models.CharField(
        max_length=40, blank=True, help_text="QuickBooks account used when a vendor has no mapping"
    )
    company_name = models.CharField(max_length=200, blank=True)
    home_currency = models.CharField(max_length=3, blank=True)
    multicurrency = models.BooleanField(default=False)
    needs_reconnect = models.BooleanField(default=False, help_text="Intuit rejected the refresh token; an admin must connect again")
    last_error = models.TextField(blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)

    # Shared with XeroConnection, so pages and posting can treat either one as "the accounting system".
    system = "quickbooks"
    system_name = "QuickBooks"
    tenant_ready = True

    @property
    def reconnect_due_soon(self) -> bool:
        return bool(self.refresh_expires_at and self.refresh_expires_at < timezone.now() + timezone.timedelta(days=30))

    @property
    def access_valid(self) -> bool:
        return self.access_expires_at > timezone.now() + timezone.timedelta(seconds=60)

    @property
    def default_account(self) -> str:
        return self.default_expense_account_id


class XeroConnection(models.Model):
    """OAuth tokens for one organization's Xero organisation (tenant).

    Xero access tokens last 30 minutes; refresh tokens rotate on every use and expire after 60 days without
    one. A sign-in can reach several Xero organisations: `tenants` lists them until an admin picks one, and
    `tenant_id` stays empty (nothing can post) until then.
    """

    class BillStatus(models.TextChoices):
        DRAFT = "DRAFT", "Draft (approve in Xero)"
        AUTHORISED = "AUTHORISED", "Awaiting payment (approved)"

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="xero")
    tenant_id = models.CharField(max_length=64, blank=True, help_text="Xero organisation (tenant) bills post to")
    tenant_name = models.CharField(max_length=200, blank=True)
    connection_id = models.CharField(max_length=64, blank=True, help_text="Xero connection id for this tenant")
    tenants = models.JSONField(default=list, blank=True,
                               help_text="Organisations this sign-in can reach: [{id, tenantId, tenantName}]")
    access_token = EncryptedTextField()
    refresh_token = EncryptedTextField()
    access_expires_at = models.DateTimeField()
    refresh_expires_at = models.DateTimeField(null=True, blank=True)
    default_account_code = models.CharField(max_length=20, blank=True,
                                            help_text="Xero account code used when a vendor has no rule")
    bill_status = models.CharField(max_length=12, choices=BillStatus.choices, default=BillStatus.DRAFT)
    home_currency = models.CharField(max_length=3, blank=True, help_text="Xero base currency")
    currencies = models.JSONField(default=list, blank=True, help_text="Currencies added in Xero")
    short_code = models.CharField(max_length=20, blank=True, help_text="Xero organisation short code (for links)")
    needs_reconnect = models.BooleanField(default=False, help_text="Xero rejected the refresh token; an admin must connect again")
    previous_tenant_id = models.CharField(
        max_length=64, blank=True, help_text="Organisation bills went to before a new sign-in; cleared once one is chosen")
    last_error = models.TextField(blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)

    system = "xero"
    system_name = "Xero"
    multicurrency = property(lambda self: len({c for c in (self.currencies or []) if c} | {self.home_currency}) > 1)

    def __str__(self) -> str:
        return f"Xero {self.tenant_name or self.tenant_id or '(choosing organisation)'}"

    @property
    def company_name(self) -> str:
        return self.tenant_name

    @property
    def tenant_ready(self) -> bool:
        return bool(self.tenant_id)

    @property
    def other_currencies(self) -> list[str]:
        return [c for c in self.currencies or [] if c and c != self.home_currency]

    @property
    def access_valid(self) -> bool:
        return self.access_expires_at > timezone.now() + timezone.timedelta(seconds=60)

    @property
    def reconnect_due_soon(self) -> bool:
        return bool(self.refresh_expires_at and self.refresh_expires_at < timezone.now() + timezone.timedelta(days=7))

    @property
    def default_account(self) -> str:
        return self.default_account_code


class VendorMapping(models.Model):
    """Learned per-client rule: this vendor maps to this vendor (QuickBooks) or contact (Xero) and expense
    account. Each system keeps its own IDs, so switching between QuickBooks and Xero never mixes them up."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="vendor_mappings")
    vendor_key = models.CharField(max_length=200)
    display_name = models.CharField(max_length=200)
    qbo_vendor_id = models.CharField(max_length=40, blank=True)
    expense_account_id = models.CharField(max_length=40, blank=True)
    expense_account_name = models.CharField(max_length=200, blank=True)
    xero_contact_id = models.CharField(max_length=64, blank=True)
    xero_account_code = models.CharField(max_length=20, blank=True)
    xero_account_name = models.CharField(max_length=200, blank=True)
    learned_from_correction = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "vendor_key")]

    def __str__(self) -> str:
        return f"{self.display_name} → {self.expense_account_name or self.expense_account_id or '?'}"


class PostedBill(models.Model):
    """One document posted to the accounting system: a Bill (QuickBooks) or ACCPAY invoice (Xero) for an
    invoice, a VendorCredit (QuickBooks) or ACCPAYCREDIT credit note (Xero) for a credit note.

    `qbo_bill_id` and `qbo_attachable_id` hold the IDs in whichever system `system` names (the field names
    predate Xero). The payment fields are filled by apps.accounting.services.payments."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        POSTED = "posted", "Posted"
        FAILED = "failed", "Failed"

    class Kind(models.TextChoices):
        BILL = "bill", "Bill"
        VENDOR_CREDIT = "vendor_credit", "Vendor credit"

    class System(models.TextChoices):
        QUICKBOOKS = "quickbooks", "QuickBooks"
        XERO = "xero", "Xero"

    class Payment(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PARTLY_PAID = "partly_paid", "Partly paid"
        PAID = "paid", "Paid"
        VOIDED = "voided", "Voided"
        DELETED = "deleted", "Deleted"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="posted_bills")
    document = models.OneToOneField(Document, on_delete=models.PROTECT, related_name="posted_bill")
    shipment = models.ForeignKey(Shipment, on_delete=models.PROTECT, related_name="posted_bills")
    request_id = models.CharField(max_length=60, unique=True,
                                  help_text="Idempotency key (QuickBooks requestid, Xero Idempotency-Key)")
    system = models.CharField(max_length=12, choices=System.choices, default=System.QUICKBOOKS)
    ledger_id = models.CharField(max_length=64, blank=True,
                                 help_text="QuickBooks company (realm) or Xero organisation (tenant) it was posted to")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.BILL)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    qbo_bill_id = models.CharField(max_length=40, blank=True,
                                   help_text="ID in the accounting system: QuickBooks Bill or VendorCredit Id, "
                                             "Xero InvoiceID or CreditNoteID")
    qbo_attachable_id = models.CharField(max_length=40, blank=True, help_text="ID of the attached PDF")
    external_number = models.CharField(max_length=60, blank=True, help_text="Bill number shown in the accounting system")
    error = models.TextField(blank=True)
    response = models.JSONField(default=dict, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Payment status, read back from the accounting system (apps.accounting.services.payments).
    payment_status = models.CharField(max_length=12, choices=Payment.choices, blank=True, db_index=True)
    amount_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    amount_paid = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True,
                                      help_text="Paid, or for a credit: used against bills")
    amount_due = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True,
                                     help_text="Still to pay, or for a credit: not used yet")
    currency = models.CharField(max_length=3, blank=True)
    due_date = models.DateField(null=True, blank=True)
    paid_on = models.DateField(null=True, blank=True, help_text="Date of the payment that settled it")
    payments = models.JSONField(default=list, blank=True,
                                help_text="[{date, amount, kind: payment|credit, reference}] as the system reports them")
    payment_checked_at = models.DateTimeField(null=True, blank=True)
    payment_changed_at = models.DateTimeField(null=True, blank=True)
    payment_error = models.CharField(max_length=500, blank=True)

    class Meta:
        indexes = [models.Index(fields=["organization", "status", "payment_status"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.display_number or '(not created)'} in {self.get_system_display()}"

    @property
    def is_credit(self) -> bool:
        return self.kind == self.Kind.VENDOR_CREDIT

    @property
    def display_number(self) -> str:
        """What a bookkeeper searches for: the bill number, else the system's own ID."""
        return self.external_number or self.qbo_bill_id

    @property
    def is_overdue(self) -> bool:
        return (not self.is_credit and self.payment_status in (self.Payment.UNPAID, self.Payment.PARTLY_PAID)
                and self.due_date is not None and self.due_date < timezone.localdate())

    @property
    def days_overdue(self) -> int:
        return (timezone.localdate() - self.due_date).days if self.is_overdue else 0

    @property
    def is_gone(self) -> bool:
        """Voided or deleted in the accounting system: it will not be paid as approved."""
        return self.payment_status in (self.Payment.VOIDED, self.Payment.DELETED)

    @property
    def payment_key(self) -> str:
        """unpaid, partly_paid, paid, overdue, voided, deleted, or '' when never checked."""
        return "overdue" if self.is_overdue else self.payment_status

    @property
    def payment_label(self) -> str:
        if not self.payment_status:
            return "Not checked yet"
        if self.is_credit:
            return {self.Payment.UNPAID: "Not used yet", self.Payment.PARTLY_PAID: "Partly used",
                    self.Payment.PAID: "Used in full"}.get(self.payment_status, self.get_payment_status_display())
        if self.is_overdue:
            return "Overdue" if self.payment_status == self.Payment.UNPAID else "Partly paid, overdue"
        return self.get_payment_status_display()


class PaymentSync(models.Model):
    """When an organization's payment statuses were last read from its accounting system."""

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="payment_sync")
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_finished_at = models.DateTimeField(null=True, blank=True)
    last_auto_at = models.DateTimeField(null=True, blank=True, help_text="Last scheduled check (at most hourly)")
    running_since = models.DateTimeField(null=True, blank=True, help_text="Set while a check runs, so two never overlap")
    paused_until = models.DateTimeField(null=True, blank=True, help_text="Daily API limit reached: wait until then")
    last_summary = models.JSONField(default=dict, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
