"""Outgoing webhooks: endpoints an organization registers, the events sent to them and every delivery attempt."""
from __future__ import annotations

import uuid
from urllib.parse import urlsplit

from django.conf import settings
from django.db import models

from apps.core.crypto import EncryptedTextField
from apps.core.models import Organization


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"


class WebhookEndpoint(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="webhook_endpoints")
    url = EncryptedTextField(help_text="https address (encrypted: it may carry a token)")
    description = models.CharField(max_length=200, blank=True)
    events = models.JSONField(default=list, blank=True, help_text="Event types this endpoint receives")
    secret = EncryptedTextField(help_text="Signing secret (whsec_...), shown once")
    previous_secret = EncryptedTextField(blank=True)
    previous_secret_expires_at = models.DateTimeField(null=True, blank=True)
    enabled = models.BooleanField(default=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    disabled_at = models.DateTimeField(null=True, blank=True)
    disabled_reason = models.CharField(max_length=300, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"{self.host} ({self.organization})"

    @property
    def host(self) -> str:
        try:
            return urlsplit(self.url or "").hostname or ""
        except ValueError:
            return ""

    @property
    def display_url(self) -> str:
        """The address without query strings or long tokens in the path."""
        try:
            parts = urlsplit(self.url or "")
        except ValueError:
            return ""
        path = parts.path or "/"
        if len(path) > 32:
            path = path[:24] + "…" + path[-6:]
        port = f":{parts.port}" if parts.port and parts.port != 443 else ""
        return f"https://{parts.hostname}{port}{path}{'?…' if parts.query else ''}"

    def signing_secrets(self, now=None) -> list[str]:
        from django.utils import timezone

        now = now or timezone.now()
        out = [self.secret]
        if self.previous_secret and self.previous_secret_expires_at and self.previous_secret_expires_at > now:
            out.append(self.previous_secret)
        return [s for s in out if s]


class WebhookEvent(models.Model):
    """One thing that happened, as sent: the JSON body is stored so retries and replays send exactly it."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="webhook_events")
    event_id = models.CharField(max_length=40, unique=True, default=new_event_id)
    type = models.CharField(max_length=60, db_index=True)
    payload = models.JSONField(default=dict)
    object_type = models.CharField(max_length=60, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.event_id} {self.type}"

    @property
    def pretty_payload(self) -> str:
        import json

        return json.dumps(self.payload, indent=2, ensure_ascii=False, default=str)


class WebhookDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Waiting to send"
        SENDING = "sending", "Sending"
        RETRYING = "retrying", "Will retry"
        SUCCEEDED = "succeeded", "Delivered"
        FAILED = "failed", "Failed"

    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name="deliveries")
    event = models.ForeignKey(WebhookEvent, on_delete=models.CASCADE, related_name="deliveries")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True, help_text="The first part of the answer")
    error = models.CharField(max_length=500, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_test = models.BooleanField(default=False)
    replay_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replays")
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "webhook deliveries"

    def __str__(self) -> str:
        return f"{self.event.type} to {self.endpoint_id}: {self.status}"


class IssueAnnouncement(models.Model):
    """Which validation issues were already sent as issue.created (issues are re-created on every check)."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    shipment_id = models.PositiveBigIntegerField()
    fingerprint = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("shipment_id", "fingerprint")]
