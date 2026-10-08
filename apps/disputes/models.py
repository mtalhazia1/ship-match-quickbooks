"""Vendor disputes: asking a vendor to fix a wrong invoice and tracking the money that comes back.

A dispute covers one vendor invoice (a payable document) and one or more validation issues on it.
Each linked issue is kept as a DisputeItem with a snapshot of what was found, so the evidence stays
readable even when the shipment is checked again and its open issues are re-created.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import Organization
from apps.documents.models import Document
from apps.shipments.models import Shipment, ValidationIssue


class DisputeSettings(models.Model):
    """Per-organization defaults for dispute emails."""

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="dispute_settings")
    reply_to = models.EmailField(blank=True, help_text="Accounts payable mailbox that vendor replies go to")
    signature = models.TextField(blank=True, help_text="Closing lines of every dispute email")
    follow_up_days = models.PositiveSmallIntegerField(default=7)
    copy_reply_to = models.BooleanField(default=True, help_text="Send a copy of each dispute to the reply-to mailbox")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def for_org(cls, org: Organization) -> DisputeSettings:
        obj, _ = cls.objects.get_or_create(organization=org)
        return obj


class VendorContact(models.Model):
    """Who to write to at a vendor about billing. Remembered from the last dispute."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="vendor_contacts")
    vendor_key = models.CharField(max_length=200)
    vendor_name = models.CharField(max_length=200, blank=True)
    name = models.CharField(max_length=200, blank=True)
    email = models.EmailField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "vendor_key")]

    def __str__(self) -> str:
        return f"{self.vendor_name or self.vendor_key}: {self.email}"


class Dispute(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent to vendor"
        ACKNOWLEDGED = "acknowledged", "Vendor acknowledged"
        CREDIT_RECEIVED = "credit_received", "Credit received"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed without recovery"

    # Waiting for the vendor: these hold the shipment (unless an approver releases it) and can become overdue.
    WAITING = {Status.SENT, Status.ACKNOWLEDGED}
    OPEN = {Status.DRAFT, Status.SENT, Status.ACKNOWLEDGED, Status.CREDIT_RECEIVED}
    FINAL = {Status.RESOLVED, Status.CLOSED}
    RECOVERED = {Status.CREDIT_RECEIVED, Status.RESOLVED}

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="disputes")
    reference = models.CharField(max_length=20, blank=True, db_index=True)
    shipment = models.ForeignKey(Shipment, null=True, blank=True, on_delete=models.SET_NULL, related_name="disputes")
    shipment_reference = models.CharField(max_length=20, blank=True, help_text="Kept if the shipment is removed")
    invoice = models.ForeignKey(Document, null=True, blank=True, on_delete=models.SET_NULL, related_name="disputes")
    invoice_number = models.CharField(max_length=60, blank=True)
    vendor_name = models.CharField(max_length=200)
    vendor_key = models.CharField(max_length=200, db_index=True)
    contact_name = models.CharField(max_length=200, blank=True)
    vendor_email = models.EmailField(blank=True)
    cc = models.CharField(max_length=500, blank=True, help_text="Extra recipients, separated by commas")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    amount_disputed = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    amount_recovered = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    amount_recovered_home = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
        help_text="Recovered amount in the home currency at the rate set when the credit was recorded")
    currency = models.CharField(max_length=3, blank=True)
    credit_note = models.ForeignKey(Document, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="credited_disputes")
    recovered_at = models.DateTimeField(null=True, blank=True, db_index=True)

    subject = models.CharField(max_length=250, blank=True)
    body = models.TextField(blank=True)
    ai_polished = models.BooleanField(default=False)
    message_id = models.CharField(max_length=250, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+")
    follow_up_on = models.DateField(null=True, blank=True)
    overdue_flagged_for = models.DateField(null=True, blank=True,
                                           help_text="Follow-up date that was last reported as overdue")

    hold_released = models.BooleanField(default=False)
    hold_released_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name="+")
    hold_released_at = models.DateTimeField(null=True, blank=True)
    hold_release_note = models.CharField(max_length=500, blank=True)

    closed_at = models.DateTimeField(null=True, blank=True)
    outcome_note = models.CharField(max_length=500, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["organization", "status"])]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.reference:
            self.reference = f"DSP-{self.pk:06d}"
            super().save(update_fields=["reference"])

    def __str__(self) -> str:
        return self.reference or f"Dispute {self.pk}"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN

    @property
    def is_waiting(self) -> bool:
        return self.status in self.WAITING

    @property
    def holds_shipment(self) -> bool:
        """A dispute waiting for the vendor keeps its shipment from being approved, unless released."""
        return self.is_waiting and not self.hold_released

    @property
    def is_overdue(self) -> bool:
        """Still waiting the day after the follow-up date (in the organization's time zone)."""
        from .savings import org_today

        return bool(self.is_waiting and self.follow_up_on and self.follow_up_on < org_today(self.organization))

    @property
    def days_open(self) -> int | None:
        """Days since the dispute was sent; stops counting when the credit arrives or it is closed."""
        if not self.sent_at:
            return None
        end = timezone.now()
        if self.status in self.FINAL or self.status in self.RECOVERED:
            end = self.recovered_at or self.closed_at or end
        return max(0, (end - self.sent_at).days)

    @property
    def outstanding(self) -> Decimal:
        return max(Decimal("0.00"), self.amount_disputed - self.amount_recovered)

    @property
    def cc_list(self) -> list[str]:
        return [a.strip() for a in self.cc.replace(";", ",").split(",") if a.strip()]


class DisputeItem(models.Model):
    """One validation issue raised with the vendor, with a snapshot of what was found."""

    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name="items")
    issue = models.ForeignKey(ValidationIssue, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="dispute_items")
    fingerprint = models.CharField(max_length=200, db_index=True)
    code = models.CharField(max_length=40)
    title = models.CharField(max_length=200)
    explanation = models.TextField(help_text="What is wrong, in words the vendor understands")
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.dispute}: {self.title}"


class DisputeEvent(models.Model):
    class Kind(models.TextChoices):
        CREATED = "created", "Draft created"
        SENT = "sent", "Sent to vendor"
        SEND_FAILED = "send_failed", "Sending failed"
        REPLY = "reply", "Vendor reply logged"
        CREDIT = "credit", "Credit linked"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed without recovery"
        RELEASED = "released", "Shipment released for approval"
        FOLLOW_UP = "follow_up", "Follow-up date changed"
        OVERDUE = "overdue", "Follow-up date passed"
        NOTE = "note", "Note"

    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    text = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    VERBS = {
        "created": "created the draft", "sent": "sent the dispute to the vendor", "send_failed": "couldn't send it",
        "reply": "logged the vendor's reply", "credit": "recorded a credit", "resolved": "marked it resolved",
        "closed": "closed it without recovery", "released": "let the shipment be approved without waiting",
        "follow_up": "changed the follow-up date", "overdue": "flagged that the follow-up date passed",
        "note": "added a note",
    }

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.dispute} {self.kind}"

    @property
    def verb(self) -> str:
        return self.VERBS.get(self.kind, self.get_kind_display().lower())
