"""Alert channels (Slack, Microsoft Teams, email) and the log of every message sent to them."""
from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.crypto import EncryptedTextField
from apps.core.models import Organization

from .events import EVENT_LABELS


class Channel(models.Model):
    class Kind(models.TextChoices):
        SLACK = "slack", "Slack"
        TEAMS = "teams", "Microsoft Teams"
        EMAIL = "email", "Email"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="alert_channels")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    name = models.CharField(max_length=100)
    webhook_url = EncryptedTextField(blank=True, help_text="Slack incoming webhook or Teams workflow URL (encrypted)")
    email_recipients = models.TextField(blank=True, help_text="Addresses separated by commas")
    enabled = models.BooleanField(default=True)
    events = models.JSONField(default=list, blank=True, help_text="Event keys this channel receives")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_kind_display()})"

    @property
    def is_webhook(self) -> bool:
        return self.kind in {self.Kind.SLACK, self.Kind.TEAMS}

    @property
    def recipients(self) -> list[str]:
        return [a.strip() for a in self.email_recipients.replace(";", ",").replace("\n", ",").split(",") if a.strip()]

    @property
    def destination(self) -> str:
        """Where messages go, without revealing the secret part of a webhook URL."""
        if self.kind == self.Kind.EMAIL:
            return ", ".join(self.recipients)
        from urllib.parse import urlsplit

        url = self.webhook_url or ""
        host = urlsplit(url).hostname or ""
        return f"{host}/…{url[-4:]}" if host else "No address"

    def wants(self, event: str) -> bool:
        return self.enabled and event in (self.events or [])

    @property
    def event_labels(self) -> list[str]:
        return [EVENT_LABELS[e] for e in self.events or [] if e in EVENT_LABELS]


class NotificationSettings(models.Model):
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="notification_settings")
    digest_hour = models.PositiveSmallIntegerField(default=8, help_text="Local hour (0-23) for the daily summary")
    last_digest_on = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def for_org(cls, org: Organization) -> NotificationSettings:
        obj, _ = cls.objects.get_or_create(organization=org)
        return obj


class Delivery(models.Model):
    """One message to one channel. Retried with backoff when the service is busy or down."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting to send"
        SENDING = "sending", "Sending"
        RETRYING = "retrying", "Will retry"
        SENT = "sent", "Delivered"
        FAILED = "failed", "Failed"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="alert_deliveries")
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="deliveries")
    event = models.CharField(max_length=40, db_index=True)
    title = models.CharField(max_length=250)
    message = models.JSONField(default=dict, help_text="What was sent, so a retry sends the same thing")
    object_type = models.CharField(max_length=60, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    is_test = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "deliveries"

    def __str__(self) -> str:
        return f"{self.event} to {self.channel_id}: {self.status}"

    @property
    def event_label(self) -> str:
        return EVENT_LABELS.get(self.event, "Test message" if self.is_test else self.event)
