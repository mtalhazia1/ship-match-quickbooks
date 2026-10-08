from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import models


class Organization(models.Model):
    """One client company (tenant). Every business record belongs to exactly one."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    home_currency = models.CharField(max_length=3, default="USD")
    timezone = models.CharField(max_length=64, default="UTC",
                                help_text="Used for dates when a user's own time zone is unknown")
    require_mfa = models.BooleanField(default=False, help_text="Every member must use two-factor authentication")
    maker_checker = models.BooleanField(
        default=True, help_text="A person who edited a shipment cannot approve it")
    review_threshold = models.FloatField(
        null=True, blank=True, help_text="Fields below this confidence go to review (default from settings)")
    fx_rates = models.JSONField(
        default=dict, blank=True,
        help_text='Home-currency value of one unit of each foreign currency, e.g. {"EUR": "1.08"}')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.name

    def to_home(self, amount: Decimal, currency: str | None) -> Decimal | None:
        """Convert an amount to the home currency. None if no rate is configured."""
        cur = (currency or self.home_currency).upper()
        if cur == self.home_currency:
            return amount
        raw = (self.fx_rates or {}).get(cur)
        try:
            return (amount * Decimal(str(raw))).quantize(Decimal("0.01")) if raw else None
        except (InvalidOperation, ValueError):
            return None


class Membership(models.Model):
    """Which users may work on which organization, and what they may do."""

    class Role(models.TextChoices):
        VIEWER = "viewer", "Viewer"
        REVIEWER = "reviewer", "Reviewer"
        APPROVER = "approver", "Approver"
        ADMIN = "admin", "Admin"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.REVIEWER)
    approval_limit = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
        help_text="Largest shipment total (home currency) this person may approve. Empty = no limit.")
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    class Meta:
        unique_together = [("user", "organization")]

    def __str__(self) -> str:
        return f"{self.user} @ {self.organization} ({self.role})"


class AuditEvent(models.Model):
    """Append-only record of every extraction, edit, approval, sign-in and posting."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="audit_events",
                                     null=True, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=60, db_index=True)
    object_type = models.CharField(max_length=60)
    object_id = models.CharField(max_length=64)
    data = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
            models.Index(fields=["object_type", "object_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.object_type}#{self.object_id}"
