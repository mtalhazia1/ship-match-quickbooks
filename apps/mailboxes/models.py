"""Mailboxes that deliver documents by email, and a record of every email they delivered.

Kinds:
  * inbound   - a forwarding address <org-slug>-<token>@INBOUND_EMAIL_DOMAIN; Postmark or Mailgun post each
                email to our webhook. One per organization.
  * microsoft - a Microsoft 365 / Outlook.com mailbox read through Microsoft Graph (OAuth).
  * imap      - any mailbox reachable over IMAP with a username and password.
  * gmail     - the Gmail label authorized with `manage.py gmail_auth` (token file on the server).

Each email is stored once per organization (IngestedEmail, unique by Message-ID) with a MailboxMessage
saying which mailbox delivered it and what happened, and one EmailAttachment row per attachment.
"""
from __future__ import annotations

import re

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.crypto import EncryptedTextField
from apps.core.models import Organization
from apps.documents.models import Document, IngestedEmail

_SPLIT = re.compile(r"[\s,;]+")


class Mailbox(models.Model):
    class Kind(models.TextChoices):
        INBOUND = "inbound", "Forwarding address"
        MICROSOFT = "microsoft", "Microsoft 365"
        IMAP = "imap", "IMAP"
        GMAIL = "gmail", "Gmail"

    class Security(models.TextChoices):
        SSL = "ssl", "SSL/TLS (usually port 993)"
        STARTTLS = "starttls", "STARTTLS (usually port 143)"

    class AfterImport(models.TextChoices):
        CATEGORY = "category", "Add the ShipMatch category"
        MOVE = "move", "Move it to another folder"
        CATEGORY_MOVE = "category_move", "Add the category and move it"
        NONE = "none", "Leave it as it is"

    POLLED_KINDS = (Kind.MICROSOFT, Kind.IMAP, Kind.GMAIL)

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="mailboxes")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    display_name = models.CharField(max_length=120, blank=True)
    address = models.CharField(max_length=320, blank=True, help_text="The mailbox's email address")
    inbound_local = models.CharField(
        max_length=80, unique=True, null=True, blank=True,
        help_text="Local part of the forwarding address (before the @); the random part makes it unguessable")

    # IMAP connection
    host = models.CharField(max_length=255, blank=True)
    port = models.PositiveIntegerField(null=True, blank=True)
    security = models.CharField(max_length=10, choices=Security.choices, default=Security.SSL)
    username = models.CharField(max_length=320, blank=True)
    password = EncryptedTextField(blank=True)

    # OAuth (Microsoft 365)
    access_token = EncryptedTextField(blank=True)
    refresh_token = EncryptedTextField(blank=True)
    access_expires_at = models.DateTimeField(null=True, blank=True)

    # Where to read and what to do afterwards
    folder = models.CharField(max_length=255, blank=True,
                              help_text="IMAP folder name, Microsoft folder ID or Gmail label")
    folder_name = models.CharField(max_length=255, blank=True)
    folder_options = models.JSONField(default=list, blank=True,
                                      help_text="Folders offered in the settings (Microsoft 365)")
    after_import = models.CharField(max_length=20, choices=AfterImport.choices, default=AfterImport.CATEGORY)
    processed_folder = models.CharField(max_length=255, blank=True)
    processed_folder_name = models.CharField(max_length=255, blank=True)
    mark_seen = models.BooleanField(default=False, help_text="IMAP: mark imported emails as read")
    allowed_senders = models.TextField(
        blank=True, help_text="Only accept email from these addresses or domains (one per line). Empty = anyone.")

    # Health
    enabled = models.BooleanField(default=True)
    needs_reconnect = models.BooleanField(default=False)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_received_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    # Where the next check starts: IMAP UID, Microsoft receivedDateTime, Gmail internal date (seconds)
    cursor = models.CharField(max_length=2000, blank=True)
    cursor_validity = models.CharField(max_length=40, blank=True, help_text="IMAP UIDVALIDITY the cursor belongs to")

    emails_received = models.PositiveIntegerField(default=0)
    documents_received = models.PositiveIntegerField(default=0)
    attachments_skipped = models.PositiveIntegerField(default=0)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "display_name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["organization"], condition=Q(kind="inbound"),
                                    name="mailbox_one_forwarding_address_per_org"),
        ]
        indexes = [models.Index(fields=["enabled", "kind"])]

    def __str__(self) -> str:
        return self.label

    @property
    def label(self) -> str:
        return self.display_name or self.address or self.get_kind_display()

    @property
    def is_polled(self) -> bool:
        return self.kind in self.POLLED_KINDS

    @property
    def inbound_address(self) -> str:
        if self.kind != self.Kind.INBOUND or not self.inbound_local or not settings.INBOUND_EMAIL_DOMAIN:
            return ""
        return f"{self.inbound_local}@{settings.INBOUND_EMAIL_DOMAIN}"

    @property
    def access_valid(self) -> bool:
        return bool(self.access_token and self.access_expires_at
                    and self.access_expires_at > timezone.now() + timezone.timedelta(seconds=90))

    @property
    def status(self) -> str:
        """ok | paused | reconnect | error | new: drives the badge on the settings page."""
        if not self.enabled:
            return "paused"
        if self.needs_reconnect:
            return "reconnect"
        if self.last_error:
            return "error"
        if self.is_polled and not self.last_checked_at:
            return "new"
        return "ok"

    @property
    def status_label(self) -> str:
        return {"paused": "Paused", "reconnect": "Needs reconnecting", "error": "Problem", "new": "Not checked yet",
                "ok": "Receiving" if self.kind == self.Kind.INBOUND else "Working"}[self.status]

    @property
    def status_badge(self) -> str:
        return {"ok": "ok", "new": "neutral", "paused": "neutral", "error": "warn", "reconnect": "err"}[self.status]

    @property
    def reads(self) -> str:
        """What the mailbox reads, for the settings table."""
        if self.kind == self.Kind.GMAIL:
            return f"Label {self.folder_name or self.folder}"
        return self.folder_name or self.folder or "Inbox"

    @property
    def afterwards(self) -> str:
        if self.kind == self.Kind.MICROSOFT:
            return {self.AfterImport.CATEGORY: "Adds the ShipMatch category",
                    self.AfterImport.MOVE: f"Moves to {self.processed_folder_name or 'another folder'}",
                    self.AfterImport.CATEGORY_MOVE: f"Adds the category, moves to {self.processed_folder_name or 'another folder'}",
                    self.AfterImport.NONE: "Leaves emails as they are"}.get(self.after_import, "")
        if self.kind == self.Kind.IMAP:
            return "Marks imported emails as read" if self.mark_seen else "Leaves emails unread"
        if self.kind == self.Kind.GMAIL:
            return "Read only"
        return ""

    # ---- allowed senders

    def sender_rules(self) -> list[str]:
        return [r.lower() for r in _SPLIT.split(self.allowed_senders or "") if r.strip()]

    def sender_allowed(self, address: str) -> bool:
        rules = self.sender_rules()
        if not rules:
            return True
        addr = (address or "").strip().lower()
        if "@" not in addr:
            return False
        domain = addr.rsplit("@", 1)[1]
        for rule in rules:
            if "@" in rule and not rule.startswith("@"):
                if addr == rule:
                    return True
                continue
            d = rule.lstrip("@").removeprefix("*.").strip(".")
            if d and (domain == d or domain.endswith("." + d)):
                return True
        return False


class MailboxMessage(models.Model):
    """One email received through a mailbox: who sent it, where it came in, and what came of it."""

    class Outcome(models.TextChoices):
        DOCUMENTS = "documents", "Documents added"
        DUPLICATES = "duplicates", "Files received before"
        NOTHING = "nothing", "No documents in it"
        BLOCKED = "blocked", "Sender not allowed"
        FAILED = "failed", "Couldn't be processed"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="mailbox_messages")
    mailbox = models.ForeignKey(Mailbox, null=True, blank=True, on_delete=models.SET_NULL, related_name="messages")
    mailbox_name = models.CharField(max_length=160, blank=True, help_text="Kept when the mailbox is removed")
    mailbox_kind = models.CharField(max_length=20, choices=Mailbox.Kind.choices, blank=True)
    email = models.OneToOneField(IngestedEmail, on_delete=models.CASCADE, related_name="intake")
    recipient = models.CharField(max_length=320, blank=True)
    provider_ref = models.CharField(max_length=255, blank=True, help_text="UID, Microsoft or provider message ID")
    outcome = models.CharField(max_length=20, choices=Outcome.choices, default=Outcome.NOTHING)
    note = models.TextField(blank=True)
    documents_created = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["organization", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.email.subject or '(no subject)'} from {self.email.sender}"

    @property
    def badge(self) -> str:
        return {"documents": "ok", "duplicates": "neutral", "nothing": "neutral", "blocked": "warn",
                "failed": "err"}.get(self.outcome, "neutral")

    @property
    def result_label(self) -> str:
        if self.outcome == self.Outcome.DOCUMENTS:
            n = self.documents_created
            return f"{n} document{'s' if n != 1 else ''} added"
        return self.get_outcome_display()


class EmailAttachment(models.Model):
    """What happened to one attachment of a received email."""

    class Outcome(models.TextChoices):
        IMPORTED = "imported", "Added as a document"
        DUPLICATE = "duplicate", "Same file received before"
        SKIPPED = "skipped", "Skipped"

    message = models.ForeignKey(MailboxMessage, on_delete=models.CASCADE, related_name="attachments")
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=150, blank=True)
    size = models.PositiveIntegerField(default=0)
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    reason = models.CharField(max_length=300, blank=True)
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="email_attachments")

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.filename}: {self.get_outcome_display()}"
