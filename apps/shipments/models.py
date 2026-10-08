from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.models import Organization
from apps.documents.models import Document


class Shipment(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        NEEDS_REVIEW = "needs_review", "Needs review"
        READY = "ready", "Ready to approve"
        APPROVED = "approved", "Approved"
        POSTED = "posted", "Posted to accounting"
        REJECTED = "rejected", "Rejected"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="shipments")
    reference = models.CharField(max_length=20, blank=True)
    bl_number = models.CharField(max_length=40, blank=True, db_index=True)
    container_numbers = models.JSONField(default=list, blank=True)
    po_numbers = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.reference:
            self.reference = f"SHP-{self.pk:06d}"
            super().save(update_fields=["reference"])

    def __str__(self) -> str:
        return self.reference or f"Shipment {self.pk}"

    @property
    def documents(self):
        return Document.objects.filter(match__shipment=self).order_by("received_at", "id")

    @property
    def is_locked(self) -> bool:
        """Approved or posted shipments are read-only."""
        return self.status in {self.Status.APPROVED, self.Status.POSTED}


class MatchLink(models.Model):
    """Why a document belongs to a shipment. One shipment per document."""

    class Method(models.TextChoices):
        EXACT_BL = "exact_bl", "Exact B/L number"
        EXACT_CONTAINER = "exact_container", "Exact container number"
        EXACT_PO = "exact_po", "Exact PO number"
        FUZZY = "fuzzy", "Near-match reference"
        ORIGINAL_INVOICE = "original_invoice", "Invoice it credits"
        ENTRY_NUMBER = "entry_number", "Customs entry number"
        NEW = "new_shipment", "First document of this shipment"
        MANUAL = "manual", "Assigned by reviewer"

    document = models.OneToOneField(Document, on_delete=models.CASCADE, related_name="match")
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="links")
    method = models.CharField(max_length=20, choices=Method.choices)
    score = models.FloatField()
    reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ValidationIssue(models.Model):
    class Severity(models.TextChoices):
        ERROR = "error", "Error"
        WARNING = "warning", "Warning"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="issues")
    shipment = models.ForeignKey(Shipment, null=True, blank=True, on_delete=models.CASCADE, related_name="issues")
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.CASCADE, related_name="issues")
    code = models.CharField(max_length=40, db_index=True)
    severity = models.CharField(max_length=10, choices=Severity.choices)
    message = models.CharField(max_length=500)
    fingerprint = models.CharField(max_length=200, help_text="Stable key so an acknowledged issue is not re-raised")
    data = models.JSONField(default=dict, blank=True)
    amount_at_risk = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
        help_text="Money this issue would cost if paid as invoiced (overcharge, duplicate, unapproved charge)")
    currency = models.CharField(max_length=3, blank=True)
    resolved = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["resolved", "-severity", "id"]

    def save(self, *args, **kwargs):
        from apps.core.utils import clamp_money

        self.amount_at_risk = clamp_money(self.amount_at_risk)   # a bad amount must never break the pages that sum this column
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.message}"

    @property
    def title(self) -> str:
        from apps.shipments.labels import issue_title

        return issue_title(self.code)

    @property
    def guidance(self) -> str:
        from apps.shipments.labels import issue_guidance

        return issue_guidance(self.code)


class Approval(models.Model):
    class Decision(models.TextChoices):
        APPROVE = "approve", "Approved"
        REJECT = "reject", "Rejected"

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="approvals")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decision = models.CharField(max_length=10, choices=Decision.choices)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
