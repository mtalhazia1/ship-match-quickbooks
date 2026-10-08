from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.crypto import EncryptedTextField
from apps.core.models import Organization


class Profile(models.Model):
    """Per-user settings and two-factor authentication state."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    timezone = models.CharField(max_length=64, blank=True, help_text="Empty = use the browser's time zone")
    mfa_secret = EncryptedTextField(blank=True)
    mfa_enabled_at = models.DateTimeField(null=True, blank=True)
    recovery_codes = models.JSONField(default=list, blank=True, help_text="SHA-256 hashes of unused codes")

    @property
    def mfa_enabled(self) -> bool:
        return self.mfa_enabled_at is not None

    def __str__(self) -> str:
        return f"Profile of {self.user}"


class ApiKey(models.Model):
    """Organization-scoped key for integrations. Only a hash of the secret is stored."""

    class Role(models.TextChoices):
        VIEWER = "viewer", "Read only"
        REVIEWER = "reviewer", "Read and upload"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=12, unique=True)
    key_hash = models.CharField(max_length=64)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.VIEWER)
    scopes = models.JSONField(default=list, blank=True,
                              help_text="e.g. shipments:read, exports:read (apps/integrations/apiscopes.py). "
                                        "Empty = everything the access level allows (keys made before scopes)")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_active(self) -> bool:
        now = timezone.now()
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > now)

    @property
    def scope_labels(self) -> list[str]:
        from apps.integrations.apiscopes import labels

        return labels(self)

    def __str__(self) -> str:
        return f"{self.name} (sm_{self.prefix}_...)"
