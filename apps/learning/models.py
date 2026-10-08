"""What ShipMatch has learned about each vendor's documents from reviewer corrections."""
from __future__ import annotations

from django.db import models

from apps.core.models import Organization
from apps.documents.models import Document


class VendorProfile(models.Model):
    """Per organization, vendor and document type: how this vendor prints its values.

    Filled only from reviewer corrections (see services/learn.py). `labels` maps a field to the
    label printed before its value, e.g. {"invoice_number": {"label": "Ref No.", "hits": 2, ...}}.
    """

    class DateFormat(models.TextChoices):
        UNKNOWN = "", "Not learned yet"
        DAY_FIRST = "dmy", "Day first (DD/MM/YYYY)"
        MONTH_FIRST = "mdy", "Month first (MM/DD/YYYY)"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="vendor_profiles")
    vendor_key = models.CharField(max_length=200)
    doc_type = models.CharField(max_length=30, choices=Document.DocType.choices)
    display_name = models.CharField(max_length=200)
    labels = models.JSONField(default=dict, blank=True)
    date_format = models.CharField(max_length=3, choices=DateFormat.choices, blank=True, default="")
    date_votes = models.JSONField(default=dict, blank=True, help_text='{"dmy": 2, "mdy": 0}')
    field_corrections = models.JSONField(default=dict, blank=True, help_text="Corrections per field")
    examples = models.JSONField(default=list, blank=True, help_text="Most recent corrections, newest last")
    correction_count = models.PositiveIntegerField(default=0)
    last_learned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("organization", "vendor_key", "doc_type")]
        ordering = ["display_name", "doc_type"]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.get_doc_type_display()})"

    @property
    def has_knowledge(self) -> bool:
        return bool(self.labels or self.date_format or self.examples)


class DocumentLearning(models.Model):
    """Which values on a document were filled or changed by what ShipMatch learned (for the review screen)."""

    document = models.OneToOneField(Document, on_delete=models.CASCADE, related_name="learning_record")
    profile = models.ForeignKey(VendorProfile, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="documents")
    vendor_name = models.CharField(max_length=200, blank=True)
    corrections_at_read = models.PositiveIntegerField(default=0)
    # {field: {"how": "label" | "date_format" | "vendor" | "hint", "label": "Ref No.", "replaced": old value}}
    fields = models.JSONField(default=dict, blank=True)
    hint_chars = models.PositiveIntegerField(default=0, help_text="Size of the vendor notes sent to the AI reader")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Learning applied to document {self.document_id}"
