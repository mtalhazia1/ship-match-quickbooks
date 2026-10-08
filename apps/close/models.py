"""Month-end close: accrual settings, adjustments and locked versions; vendor statements and their
reconciliation; payments made to vendors.

Vendor statements are kept here, not as documents: a statement is never classified, matched to a
shipment, checked or posted. It only reads the documents ShipMatch already has.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.accounting.models import vendor_key
from apps.core.models import Organization

from . import groups


def _setting(name: str, default):
    return getattr(settings, name, default)


def _accrued_default() -> str:
    return _setting("CLOSE_ACCRUED_LIABILITIES_ACCOUNT", "Accrued liabilities")


def _freight_default() -> str:
    return _setting("CLOSE_FREIGHT_ACCOUNT", "Freight expense")


def _goods_default() -> str:
    return _setting("CLOSE_GOODS_ACCOUNT", "Inventory")


def _lookback_default() -> int:
    return int(_setting("CLOSE_LOOKBACK_DAYS", 180))


def _history_default() -> int:
    return int(_setting("CLOSE_MIN_HISTORY", 3))


class CloseSettings(models.Model):
    """How one organization's month-end accruals are worked out and booked."""

    class Expect(models.TextChoices):
        ALWAYS = "always", "Every shipment"
        USUAL = "usual", "When most similar shipments have it"
        NEVER = "never", "Never"

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="close_settings")
    accrued_account = models.CharField(max_length=200, default=_accrued_default,
                                       help_text="Account credited by the accrual (a liability account)")
    accrued_account_id = models.CharField(max_length=40, blank=True, help_text="Its QuickBooks account ID, if known")
    freight_account = models.CharField(max_length=200, default=_freight_default,
                                       help_text="Account debited for freight when the vendor has no account set")
    goods_account = models.CharField(max_length=200, default=_goods_default,
                                     help_text="Account debited for supplier (goods) invoices without an account set")
    include_goods = models.BooleanField(
        default=True, help_text="Also accrue supplier (goods) invoices received but not booked, debited to the goods "
                                "account")
    expect_freight = models.CharField(max_length=10, choices=Expect.choices, default=Expect.ALWAYS)
    expect_destination = models.CharField(max_length=10, choices=Expect.choices, default=Expect.ALWAYS)
    expect_delivery = models.CharField(max_length=10, choices=Expect.choices, default=Expect.USUAL)
    lookback_days = models.PositiveIntegerField(
        default=_lookback_default,
        help_text="Shipments that shipped longer ago than this before the period end are listed, not accrued")
    min_history = models.PositiveSmallIntegerField(
        default=_history_default, help_text="Past invoices needed before a median is used as an estimate")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def for_org(cls, org: Organization) -> CloseSettings:
        return cls.objects.filter(organization=org).first() or cls(organization=org)

    def expectation(self, group: str) -> str:
        return {groups.FREIGHT: self.expect_freight, groups.DESTINATION: self.expect_destination,
                groups.DELIVERY: self.expect_delivery}.get(group, self.Expect.NEVER)

    def as_dict(self) -> dict:
        return {
            "accrued_account": self.accrued_account, "accrued_account_id": self.accrued_account_id,
            "freight_account": self.freight_account, "goods_account": self.goods_account,
            "include_goods": self.include_goods,
            "expect_freight": self.expect_freight, "expect_destination": self.expect_destination,
            "expect_delivery": self.expect_delivery, "lookback_days": self.lookback_days,
            "min_history": self.min_history,
        }


class AccrualAdjustment(models.Model):
    """A person's decision about one shipment's missing charges: don't accrue them, or use this amount.

    It applies until an invoice for that charge group arrives (then the invoice is used)."""

    class Action(models.TextChoices):
        EXCLUDE = "exclude", "Don't accrue"
        AMOUNT = "amount", "Use this amount"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="accrual_adjustments")
    shipment = models.ForeignKey("shipments.Shipment", on_delete=models.CASCADE, related_name="accrual_adjustments")
    group = models.CharField(max_length=20)
    action = models.CharField(max_length=10, choices=Action.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    vendor_name = models.CharField(max_length=200, blank=True)
    note = models.CharField(max_length=500)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("organization", "shipment", "group")]
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.shipment} {self.group}: {self.get_action_display()}"

    @property
    def group_label(self) -> str:
        return groups.label(self.group)


class LockedVersionError(ValidationError):
    pass


class AccrualSnapshot(models.Model):
    """The accrual report exactly as an approver locked it for a period. Never changed afterwards; running
    the period again creates the next version."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="accrual_snapshots")
    period_end = models.DateField()
    version = models.PositiveIntegerField()
    report = models.JSONField(help_text="Every line, total and setting as shown when locked")
    total = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.CharField(max_length=3)
    line_count = models.PositiveIntegerField(default=0)
    note = models.CharField(max_length=500, blank=True)
    checksum = models.CharField(max_length=64, help_text="SHA-256 of the stored report, to show it was not altered")
    locked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    locked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("organization", "period_end", "version")]
        ordering = ["-period_end", "-version"]

    def __str__(self) -> str:
        return f"Accruals {self.period_end} v{self.version}"

    @staticmethod
    def checksum_for(report: dict) -> str:
        return hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @property
    def intact(self) -> bool:
        return self.checksum == self.checksum_for(self.report)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise LockedVersionError("A locked accrual version can't be changed. Run the period again to create "
                                     "a new version.")
        self.checksum = self.checksum_for(self.report)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise LockedVersionError("A locked accrual version can't be deleted.")


# --------------------------------------------------------------------------- vendor statements


def statement_path(instance: VendorStatement, filename: str) -> str:
    return f"{instance.organization.slug}/statements/{instance.sha256[:2]}/{instance.sha256[:16]}_{filename}"


class VendorStatement(models.Model):
    class Status(models.TextChoices):
        READY = "ready", "Reconciled"
        NEEDS_VENDOR = "needs_vendor", "Choose the vendor"
        UNREADABLE = "unreadable", "Couldn't be read"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="vendor_statements")
    vendor_name = models.CharField(max_length=200, blank=True)
    vendor_key = models.CharField(max_length=200, blank=True, db_index=True)
    statement_date = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    opening_balance = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    closing_balance = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True,
                                          help_text="Balance printed on the statement")
    file = models.FileField(upload_to=statement_path, max_length=500)
    original_filename = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    source_format = models.CharField(max_length=12, help_text="pdf, xlsx or csv")
    text = models.TextField(blank=True)
    reader = models.CharField(max_length=20, blank=True, help_text="rules, or the AI provider that read it")
    notes = models.JSONField(default=list, blank=True, help_text="What the reader could not read, for the reviewer")
    llm_usage = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.READY, db_index=True)
    error = models.TextField(blank=True)
    summary = models.JSONField(default=dict, blank=True, help_text="Balances and differences from the last match")
    reconciled_at = models.DateTimeField(null=True, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "sha256")]
        ordering = ["-statement_date", "-created_at", "-id"]

    def __str__(self) -> str:
        return f"Statement {self.vendor_name or '?'} {self.statement_date or ''}".strip()

    def set_vendor(self, name: str) -> None:
        self.vendor_name = (name or "").strip()[:200]
        self.vendor_key = vendor_key(self.vendor_name)

    @property
    def open_items(self) -> int:
        return self.items.exclude(bucket__in=ReconItem.QUIET).filter(resolved=False).count()


class StatementLine(models.Model):
    """One line of a vendor statement. `amount` is signed as it moves the vendor's balance: invoices are
    positive, credits and payments negative; an opening balance keeps its own sign."""

    class Kind(models.TextChoices):
        INVOICE = "invoice", "Invoice"
        CREDIT = "credit", "Credit note"
        PAYMENT = "payment", "Payment"
        OPENING = "opening", "Opening balance"

    statement = models.ForeignKey(VendorStatement, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveIntegerField()
    kind = models.CharField(max_length=10, choices=Kind.choices)
    number = models.CharField(max_length=80, blank=True)
    date = models.DateField(null=True, blank=True)
    reference = models.CharField(max_length=200, blank=True)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    balance = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    raw = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.number} {self.amount}"

    @property
    def magnitude(self) -> Decimal:
        return abs(self.amount)


class VendorPayment(models.Model):
    """Money paid to a vendor, entered by a person or read from QuickBooks bill payments."""

    class Source(models.TextChoices):
        MANUAL = "manual", "Entered by a person"
        QUICKBOOKS = "quickbooks", "QuickBooks"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="vendor_payments")
    vendor_name = models.CharField(max_length=200)
    vendor_key = models.CharField(max_length=200, db_index=True)
    paid_on = models.DateField()
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.CharField(max_length=3)
    reference = models.CharField(max_length=100, blank=True, help_text="Check, wire or payment number")
    allocations = models.JSONField(default=list, blank=True,
                                   help_text='Invoices it paid: [{"document_id", "invoice_number", "amount"}]')
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.MANUAL)
    qbo_id = models.CharField(max_length=40, blank=True)
    note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_on", "-id"]
        constraints = [models.UniqueConstraint(fields=["organization", "qbo_id"], condition=~Q(qbo_id=""),
                                               name="close_payment_unique_qbo_id")]

    def __str__(self) -> str:
        return f"{self.vendor_name} {self.currency} {self.amount} on {self.paid_on}"

    def save(self, *args, **kwargs):
        self.vendor_name = self.vendor_name.strip()[:200]
        self.vendor_key = vendor_key(self.vendor_name)
        self.currency = (self.currency or "").strip().upper()[:3]
        super().save(*args, **kwargs)

    @property
    def invoice_numbers(self) -> list[str]:
        return [a.get("invoice_number") for a in self.allocations or [] if a.get("invoice_number")]


class ReconItem(models.Model):
    """One finding of a statement reconciliation. `effect` is how much this item contributes to the
    difference between the vendor's balance and ShipMatch's (statement minus ShipMatch)."""

    class Bucket(models.TextChoices):
        MATCHED = "matched", "Matched"
        AMOUNT_DIFFERS = "amount_differs", "Amount differs"
        MISSING = "missing", "On the statement, never received"
        NOT_ON_STATEMENT = "not_on_statement", "Received, not on the statement"
        CREDIT_NOT_APPLIED = "credit_not_applied", "Credit notes not applied"
        DUPLICATE = "duplicate", "Possible duplicates on the statement"
        PAYMENT_NOT_APPLIED = "payment_not_applied", "Payments the vendor hasn't applied"
        PAYMENT_UNKNOWN = "payment_unknown", "Payments ShipMatch has no record of"
        SETTLED = "settled", "Paid and closed"
        OPENING = "opening", "Opening balance differs"
        ARITHMETIC = "arithmetic", "Statement doesn't add up"

    # Buckets that need no action: shown for completeness, never counted as open.
    QUIET = {Bucket.MATCHED, Bucket.SETTLED}

    statement = models.ForeignKey(VendorStatement, on_delete=models.CASCADE, related_name="items")
    bucket = models.CharField(max_length=24, choices=Bucket.choices, db_index=True)
    fingerprint = models.CharField(max_length=200)
    line = models.ForeignKey(StatementLine, null=True, blank=True, on_delete=models.CASCADE, related_name="items")
    document = models.ForeignKey("documents.Document", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+")
    payment = models.ForeignKey(VendorPayment, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    label = models.CharField(max_length=200, blank=True, help_text="Invoice, credit note or payment it is about")
    statement_amount = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    shipmatch_amount = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    effect = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal("0.00"))
    explanation = models.CharField(max_length=600, blank=True)
    data = models.JSONField(default=dict, blank=True)
    resolved = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("statement", "fingerprint")]
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.get_bucket_display()}: {self.label}"
