from django.contrib import admin

from .models import EmailAttachment, Mailbox, MailboxMessage


@admin.register(Mailbox)
class MailboxAdmin(admin.ModelAdmin):
    list_display = ("label", "organization", "kind", "enabled", "needs_reconnect", "last_checked_at",
                    "emails_received", "documents_received")
    list_filter = ("kind", "enabled", "needs_reconnect", "organization")
    search_fields = ("display_name", "address", "inbound_local", "host", "username")
    # Secrets are never shown or edited here.
    exclude = ("password", "access_token", "refresh_token")
    readonly_fields = ("inbound_local", "cursor", "cursor_validity", "last_checked_at", "last_success_at",
                       "last_received_at", "emails_received", "documents_received", "attachments_skipped",
                       "created_at", "updated_at")


class AttachmentInline(admin.TabularInline):
    model = EmailAttachment
    extra = 0
    fields = ("filename", "content_type", "size", "outcome", "reason", "document")
    readonly_fields = fields


@admin.register(MailboxMessage)
class MailboxMessageAdmin(admin.ModelAdmin):
    list_display = ("email", "organization", "mailbox_name", "outcome", "documents_created", "created_at")
    list_filter = ("outcome", "mailbox_kind", "organization")
    readonly_fields = ("organization", "mailbox", "mailbox_name", "mailbox_kind", "email", "recipient", "provider_ref",
                       "outcome", "note", "documents_created", "created_at")
    inlines = [AttachmentInline]
