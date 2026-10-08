"""Team workflow: who works on which shipment, comments with @mentions, in-app notifications and
per-person preferences. Every record belongs to one organization except the person's own preferences."""
from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.models import Organization
from apps.documents.models import Document
from apps.shipments.models import Shipment


class Assignment(models.Model):
    """The team member responsible for a shipment. A row with no assignee means someone unassigned it on
    purpose, so the automatic rules leave it alone."""

    class Reason(models.TextChoices):
        MANUAL = "manual", "Assigned by a person"
        ROUND_ROBIN = "round_robin", "Taking turns"
        VENDOR = "vendor", "Vendor rule"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="assignments")
    shipment = models.OneToOneField(Shipment, on_delete=models.CASCADE, related_name="assignment")
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="workflow_assignments")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    reason = models.CharField(max_length=20, choices=Reason.choices, default=Reason.MANUAL)
    assigned_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["organization", "assignee"])]

    def __str__(self) -> str:
        return f"{self.shipment} -> {self.assignee or 'nobody'}"


class AssignmentRules(models.Model):
    """How new work is shared out in one organization."""

    class Mode(models.TextChoices):
        OFF = "off", "Don't assign automatically"
        ROUND_ROBIN = "round_robin", "Take turns among reviewers"
        VENDOR = "vendor", "By vendor"

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="assignment_rules")
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.OFF)
    include_approvers = models.BooleanField(default=False, help_text="Approvers take turns too")
    vendor_fallback = models.BooleanField(
        default=True, help_text="When no vendor rule matches, take turns instead of leaving the shipment unassigned")
    last_assigned = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def for_org(cls, org: Organization) -> AssignmentRules:
        obj, _ = cls.objects.get_or_create(organization=org)
        return obj


class VendorRule(models.Model):
    """Shipments with invoices from this vendor go to this person."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="vendor_assignment_rules")
    vendor_key = models.CharField(max_length=200)
    vendor_name = models.CharField(max_length=200)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("organization", "vendor_key")]
        ordering = ["vendor_name", "id"]

    def __str__(self) -> str:
        return f"{self.vendor_name} -> {self.assignee}"


class Comment(models.Model):
    """A comment on a shipment, or on one document. Replies point at a top-level comment (one level deep)."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="comments")
    shipment = models.ForeignKey(Shipment, null=True, blank=True, on_delete=models.CASCADE, related_name="comments")
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.CASCADE, related_name="comments")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="replies")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    body = models.TextField(blank=True)
    mentions = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["organization", "shipment"]), models.Index(fields=["organization", "document"])]

    def __str__(self) -> str:
        return f"Comment {self.pk} by {self.author}"

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class Notification(models.Model):
    """Something a person should see: a mention, a reply, a shipment assigned to them. Shown under the bell."""

    class Kind(models.TextChoices):
        MENTION = "mention", "Mentioned you"
        REPLY = "reply", "Replied to you"
        ASSIGNED = "assigned", "Assigned to you"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="workflow_notifications")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="workflow_notifications")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    title = models.CharField(max_length=250)
    body = models.CharField(max_length=500, blank=True)
    url = models.CharField(max_length=500, blank=True, help_text="Path inside ShipMatch")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="+")
    object_type = models.CharField(max_length=60, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    emailed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "read_at"])]

    def __str__(self) -> str:
        return f"{self.kind} for {self.user}: {self.title}"


class Preference(models.Model):
    """A person's own workflow settings (the same in every organization)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="workflow_prefs")
    shortcuts = models.BooleanField(default=True, help_text="Keyboard shortcuts on every page")
    email_mentions = models.BooleanField(default=True, help_text="Email me when someone mentions me or replies")
    email_assignments = models.BooleanField(default=True, help_text="Email me when a shipment is assigned to me")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def for_user(cls, user) -> Preference:
        """Saved preferences, or unsaved defaults (reading never writes a row)."""
        if not getattr(user, "is_authenticated", False):
            return cls()
        cached = getattr(user, "_workflow_prefs", None)
        if cached is None:
            cached = cls.objects.filter(user=user).first() or cls(user=user)
            user._workflow_prefs = cached
        return cached
