"""Rate cards (quotes), approved extra charges, learned charge names, checking rules and the
ledger of money caught by validation."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.accounting.models import vendor_key
from apps.core.models import Organization

from . import charges, lanes


def _default_pct() -> Decimal:
    return Decimal(str(settings.RATE_TOLERANCE_PERCENT))


def _default_amount() -> Decimal:
    return Decimal(str(settings.RATE_TOLERANCE_AMOUNT))


class RateSettings(models.Model):
    """How strictly invoices are compared with quotes, per organization."""

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="rate_settings")
    tolerance_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=_default_pct,
        help_text="An overcharge is flagged only when it is more than this share of the quoted amount...")
    tolerance_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=_default_amount,
        help_text="...and more than this fixed amount (home currency). The larger of the two applies.")
    warn_no_quote = models.BooleanField(
        default=True, help_text="Warn when a vendor has quotes on file but none fits the shipment")
    check_unlisted_vendors = models.BooleanField(
        default=False, help_text="Also flag extra charges from vendors with no quotes or approved extras on file")
    ai_classify = models.BooleanField(
        default=True, help_text="Let the AI name charges the keyword table doesn't recognize (when AI reading is on)")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def for_org(cls, org: Organization) -> RateSettings:
        """The organization's settings, or unsaved defaults."""
        found = cls.objects.filter(organization=org).first()
        return found or cls(organization=org)

    def tolerance_for(self, expected: Decimal, fixed: Decimal | None = None) -> Decimal:
        """Allowed excess over an expected amount: the larger of the percentage and the fixed amount."""
        pct = (abs(expected) * self.tolerance_percent / Decimal("100")).quantize(Decimal("0.01"))
        return max(pct, self.tolerance_amount if fixed is None else fixed)


class Quote(models.Model):
    """A vendor's agreed rates for one lane and equipment type over a validity period."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="quotes")
    vendor_name = models.CharField(max_length=200)
    vendor_key = models.CharField(max_length=200, db_index=True, editable=False)
    reference = models.CharField(max_length=60, blank=True, help_text="The vendor's quote or contract number")
    origin = models.CharField(max_length=120, blank=True, help_text="Port of loading. Empty = any origin")
    origin_key = models.CharField(max_length=60, blank=True, editable=False)
    destination = models.CharField(max_length=120, blank=True, help_text="Port of discharge. Empty = any destination")
    destination_key = models.CharField(max_length=60, blank=True, editable=False)
    equipment = models.CharField(max_length=8, blank=True, choices=lanes.EQUIPMENT_CHOICES)
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True, help_text="Empty = no end date")
    currency = models.CharField(max_length=3)
    all_in = models.BooleanField(
        default=False, help_text="The ocean freight rate includes surcharges not listed separately (BAF, CAF ...)")
    notes = models.TextField(blank=True)
    archived = models.BooleanField(default=False, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["vendor_name", "origin", "destination", "-valid_from", "-id"]
        indexes = [models.Index(fields=["organization", "vendor_key", "archived"])]

    def __str__(self) -> str:
        return self.label

    def save(self, *args, **kwargs):
        self.vendor_name = self.vendor_name.strip()
        self.vendor_key = vendor_key(self.vendor_name)
        self.origin, self.destination = self.origin.strip(), self.destination.strip()
        self.origin_key = lanes.place_key(self.origin) if self.origin else ""
        self.destination_key = lanes.place_key(self.destination) if self.destination else ""
        self.currency = (self.currency or "").strip().upper()[:3]
        self.equipment = lanes.normalize_equipment(self.equipment) or "" if self.equipment else ""
        super().save(*args, **kwargs)

    @property
    def label(self) -> str:
        return f"quote {self.reference}" if self.reference else f"{self.vendor_name} quote #{self.pk or 'new'}"

    @property
    def audit_name(self) -> str:
        return f"quote {self.reference}" if self.reference else "a quote"

    @property
    def title(self) -> str:
        return self.reference or f"Quote #{self.pk}"

    @property
    def lane(self) -> str:
        return f"{self.origin or 'Any origin'} to {self.destination or 'any destination'}"

    def is_valid_on(self, day: date) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)

    def status(self, today: date | None = None) -> str:
        today = today or timezone.localdate()
        if self.archived:
            return "archived"
        if self.valid_from > today:
            return "upcoming"
        if self.valid_to and self.valid_to < today:
            return "expired"
        return "current"


class QuoteCharge(models.Model):
    class Basis(models.TextChoices):
        CONTAINER = "container", "Per container"
        SHIPMENT = "shipment", "Per shipment"
        BL = "bl", "Per bill of lading"
        KG = "kg", "Per kg"
        CBM = "cbm", "Per cbm"
        DAY = "day", "Per day"
        HOUR = "hour", "Per hour"

    quote = models.ForeignKey(Quote, on_delete=models.CASCADE, related_name="charges")
    code = models.CharField(max_length=30, choices=charges.CODE_CHOICES)
    description = models.CharField(max_length=200, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    basis = models.CharField(max_length=12, choices=Basis.choices, default=Basis.CONTAINER)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{charges.label(self.code)} {self.amount} {self.get_basis_display().lower()}"

    @property
    def code_label(self) -> str:
        return charges.label(self.code)


class ApprovedAccessorial(models.Model):
    """An extra charge a vendor may bill without asking first, within a cap and after free time."""

    class Unit(models.TextChoices):
        DAY = "day", "Per day"
        HOUR = "hour", "Per hour"
        EACH = "each", "Per occurrence"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="approved_accessorials")
    vendor_name = models.CharField(max_length=200)
    vendor_key = models.CharField(max_length=200, db_index=True, editable=False)
    code = models.CharField(max_length=30, choices=charges.ACCESSORIAL_CHOICES)
    unit = models.CharField(max_length=8, choices=Unit.choices, default=Unit.EACH)
    free_units = models.PositiveIntegerField(default=0, help_text="Free days or hours before the charge starts")
    max_per_unit = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                       help_text="Most the vendor may charge per day, hour or occurrence")
    max_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                     help_text="Most the vendor may charge in total on one invoice")
    currency = models.CharField(max_length=3)
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["vendor_name", "code", "-valid_from", "-id"]

    def __str__(self) -> str:
        return f"{self.vendor_name}: {self.code_label}"

    def save(self, *args, **kwargs):
        self.vendor_name = self.vendor_name.strip()
        self.vendor_key = vendor_key(self.vendor_name)
        self.currency = (self.currency or "").strip().upper()[:3]
        super().save(*args, **kwargs)

    @property
    def code_label(self) -> str:
        return charges.label(self.code)

    @property
    def unit_word(self) -> str:
        return {"day": "day", "hour": "hour"}.get(self.unit, "occurrence")

    def is_valid_on(self, day: date | None) -> bool:
        if day is None:
            return True
        return (self.valid_from is None or self.valid_from <= day) and (self.valid_to is None or day <= self.valid_to)

    @property
    def terms(self) -> str:
        """Plain-words summary, e.g. 'after 4 free days, up to USD 100.00 a day, at most USD 600.00'."""
        parts = []
        if self.free_units:
            parts.append(f"after {self.free_units} free {self.unit_word}{'s' if self.free_units != 1 else ''}")
        if self.max_per_unit is not None:
            per = {"day": "a day", "hour": "an hour"}.get(self.unit, "each time")
            parts.append(f"up to {self.currency} {self.max_per_unit:,.2f} {per}")
        if self.max_amount is not None:
            parts.append(f"at most {self.currency} {self.max_amount:,.2f} per invoice")
        return ", ".join(parts) or "approved with no cap"


class ChargeAlias(models.Model):
    """A charge name this organization's vendors use, and the code it means."""

    class Source(models.TextChoices):
        AI = "ai", "Named by AI"
        PERSON = "person", "Set by a person"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="charge_aliases")
    key = models.CharField(max_length=200, help_text="Charge name without amounts and counts")
    example = models.CharField(max_length=200, blank=True)
    code = models.CharField(max_length=30, choices=charges.CODE_CHOICES)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.PERSON)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "key")]
        ordering = ["key"]

    def __str__(self) -> str:
        return f"{self.key} = {self.code}"


class CaughtCharge(models.Model):
    """Ledger of money validation flagged, one row per catch, kept when issues are re-created.

    Validation deletes and re-creates open issues on every run, so issue rows can't tell the
    story on their own. A catch is identified by the document (or shipment) plus the issue code
    and its subject (e.g. `over_quote:ocean_freight`). See apps/rates/savings.py for how a
    catch's outcome is decided.
    """

    class Cleared(models.TextChoices):
        INVOICE = "invoice", "Invoice corrected or removed"
        RATES = "rates", "Rates or checking rules changed"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="caught_charges")
    scope = models.CharField(max_length=40, help_text="d<document id>, or s<shipment id> for shipment-wide issues")
    catch_key = models.CharField(max_length=160)
    code = models.CharField(max_length=40, db_index=True, help_text="Validation issue code")
    charge_code = models.CharField(max_length=30, blank=True)
    shipment = models.ForeignKey("shipments.Shipment", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+")
    document = models.ForeignKey("documents.Document", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+")
    issue_id = models.BigIntegerField(null=True, blank=True, db_index=True,
                                      help_text="Current validation issue; empty once the check no longer finds it")
    vendor_name = models.CharField(max_length=200, blank=True)
    vendor_key = models.CharField(max_length=200, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    amount_caught = models.DecimalField(max_digits=14, decimal_places=2, help_text="Largest amount flagged")
    amount_latest = models.DecimalField(max_digits=14, decimal_places=2, help_text="Amount on the latest issue")
    first_caught_at = models.DateTimeField(db_index=True)
    last_seen_at = models.DateTimeField()
    cleared_at = models.DateTimeField(null=True, blank=True)
    cleared_reason = models.CharField(max_length=10, choices=Cleared.choices, blank=True)

    class Meta:
        unique_together = [("organization", "scope", "catch_key")]
        ordering = ["-first_caught_at", "-id"]

    def save(self, *args, **kwargs):
        from apps.core.utils import clamp_money

        self.amount_caught = clamp_money(self.amount_caught)   # see ValidationIssue.save
        self.amount_latest = clamp_money(self.amount_latest)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.catch_key} {self.currency} {self.amount_caught}"
