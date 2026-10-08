from __future__ import annotations

from django.db import models

from apps.core.models import Organization


def upload_path(instance: Document, filename: str) -> str:
    return f"{instance.organization.slug}/{instance.sha256[:2]}/{instance.sha256[:16]}_{filename}"


def original_upload_path(instance: Document, filename: str) -> str:
    return f"{instance.organization.slug}/{instance.sha256[:2]}/originals/{instance.sha256[:16]}_{filename}"


class IngestedEmail(models.Model):
    """One email we have already processed, so polling never imports it twice."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="emails")
    message_id = models.CharField(max_length=255)
    subject = models.CharField(max_length=500, blank=True)
    sender = models.CharField(max_length=320, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    attachment_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("organization", "message_id")]


class Document(models.Model):
    class Source(models.TextChoices):
        EMAIL = "email", "Email"
        UPLOAD = "upload", "Upload"
        FOLDER = "folder", "Folder import"

    class DocType(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        COMMERCIAL_INVOICE = "commercial_invoice", "Commercial invoice"
        BILL_OF_LADING = "bill_of_lading", "Bill of lading"
        FREIGHT_INVOICE = "freight_invoice", "Freight invoice"
        CREDIT_NOTE = "credit_note", "Credit note"
        CUSTOMS_ENTRY = "customs_entry", "Customs entry"      # apps/customs: CBP 7501 or a customs declaration
        ARRIVAL_NOTICE = "arrival_notice", "Arrival notice"   # apps/customs: arrival notice or delivery order
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        NEEDS_OCR = "needs_ocr", "Needs OCR"
        EXTRACTED = "extracted", "Extracted"
        MATCHED = "matched", "Matched to shipment"
        UNMATCHED = "unmatched", "No shipment found"
        ERROR = "error", "Processing error"
        SPLIT = "split", "Split into separate documents"
        ARCHIVE = "archive", "Archive unpacked"

    class Format(models.TextChoices):
        PDF = "pdf", "PDF"
        IMAGE = "image", "Image"
        SPREADSHEET = "spreadsheet", "Spreadsheet"
        ARCHIVE = "archive", "ZIP archive"

    PAYABLE_TYPES = {DocType.COMMERCIAL_INVOICE, DocType.FREIGHT_INVOICE}
    # Credit notes reduce what is owed: they are posted to accounting (as vendor credits) but are not payable.
    CREDIT_TYPES = {DocType.CREDIT_NOTE}
    # Customs entries and arrival notices are neither payable nor credits: duty reaches landed cost through
    # apps/customs/services/charges.py, and arrival notices feed free time (demurrage and detention).
    # Files whose contents became other documents; they stay for audit and are never matched or posted.
    CONTAINER_STATUSES = {Status.SPLIT, Status.ARCHIVE}

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="documents")
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.UPLOAD)
    email = models.ForeignKey(IngestedEmail, null=True, blank=True, on_delete=models.SET_NULL, related_name="documents")
    original_filename = models.CharField(max_length=255)
    file = models.FileField(upload_to=upload_path, max_length=500)
    sha256 = models.CharField(max_length=64)
    page_count = models.PositiveIntegerField(default=0)
    text = models.TextField(blank=True)
    doc_type = models.CharField(max_length=30, choices=DocType.choices, default=DocType.UNKNOWN)
    classification_confidence = models.FloatField(default=0.0)
    extraction_provider = models.CharField(max_length=30, blank=True)
    text_source = models.CharField(max_length=20, blank=True, help_text="text_layer, anthropic, textract or none")
    llm_usage = models.JSONField(default=dict, blank=True, help_text="Tokens, time and estimated cost of AI calls")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RECEIVED, db_index=True)
    error = models.TextField(blank=True)
    source_format = models.CharField(max_length=20, choices=Format.choices, default=Format.PDF,
                                     help_text="What was received; images and spreadsheets are converted to a PDF copy")
    original_file = models.FileField(upload_to=original_upload_path, max_length=500, blank=True,
                                     help_text="The file exactly as received, when it is not a PDF")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="children",
                               help_text="The ZIP archive or multi-invoice PDF this document came from")
    intake = models.JSONField(default=dict, blank=True,
                              help_text="How the file was received: conversion, archive contents, split pages")
    received_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "sha256")]
        ordering = ["received_at", "id"]

    def __str__(self) -> str:
        return f"{self.original_filename} ({self.get_doc_type_display()})"

    @property
    def is_payable(self) -> bool:
        return self.doc_type in self.PAYABLE_TYPES

    @property
    def is_credit(self) -> bool:
        return self.doc_type in self.CREDIT_TYPES

    @property
    def posts_to_accounting(self) -> bool:
        """Invoices become bills, credit notes become vendor credits."""
        return self.is_payable or self.is_credit

    @property
    def is_container(self) -> bool:
        return self.status in self.CONTAINER_STATUSES

    @property
    def pdf_filename(self) -> str:
        """Name for the stored PDF copy (an image or spreadsheet keeps its own name, with .pdf)."""
        name = self.original_filename or "document.pdf"
        if name.lower().endswith(".pdf"):
            return name
        stem = name.rsplit(".", 1)[0] if "." in name else name
        return f"{stem}.pdf"

    def data(self) -> dict:
        """Extracted values as a plain dict: {field_name: value}."""
        return {f.name: f.value for f in self.fields.all()}

    def field(self, name: str, default=None):
        f = self.fields.filter(name=name).first()
        return f.value if f else default


class ExtractedField(models.Model):
    class Source(models.TextChoices):
        RULES = "rules", "Rule extractor"
        LLM = "llm", "LLM"
        HUMAN = "human", "Human correction"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="fields")
    name = models.CharField(max_length=60)
    value = models.JSONField(null=True, blank=True)
    confidence = models.FloatField(default=0.0)
    grounded = models.BooleanField(default=False, help_text="Value was found verbatim in the document text")
    page = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.RULES)
    location = models.JSONField(
        null=True, blank=True,
        help_text="Where the value is printed: {status, page, boxes: [[x0, top, x1, bottom]] as 0..1 of the page, "
                  "score, items} (see apps/documents/services/locate.py)")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("document", "name")]
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.name}={self.value!r} ({self.confidence:.2f})"


class OcrLayout(models.Model):
    """Word positions from OCR of a scanned document (AWS Textract), kept so values can be located
    on the page again after a reviewer corrects them. Text-layer PDFs don't need this."""

    document = models.OneToOneField(Document, on_delete=models.CASCADE, related_name="ocr_layout")
    provider = models.CharField(max_length=20, default="textract")
    words = models.JSONField(default=list, help_text="[[page, x0, top, x1, bottom, text], ...] with 0..1 coordinates")
    created_at = models.DateTimeField(auto_now_add=True)
