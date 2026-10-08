"""Uploads from the public try page. Each one gets its own throwaway sandbox organization."""
from __future__ import annotations

from django.db import models
from django.utils import timezone

from apps.core.models import Organization
from apps.documents.models import Document


class TrySubmission(models.Model):
    """One visitor upload. The result is reachable only with the token in its URL (we keep a hash of it)."""

    token_hash = models.CharField(max_length=64, unique=True)
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="try_submission",
                                        help_text="Sandbox organization created for this upload alone")
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    filename = models.CharField(max_length=255)
    size_bytes = models.PositiveIntegerField(default=0)
    page_count = models.PositiveIntegerField(default=0)
    ip_hash = models.CharField(max_length=32, blank=True, help_text="Keyed hash of the visitor's IP, for abuse checks")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Try upload {self.filename} ({self.created_at:%Y-%m-%d %H:%M})"

    @property
    def expired(self) -> bool:
        return self.expires_at <= timezone.now()
