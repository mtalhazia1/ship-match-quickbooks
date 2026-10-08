from django.contrib import admin

from .models import TrySubmission


@admin.register(TrySubmission)
class TrySubmissionAdmin(admin.ModelAdmin):
    list_display = ("filename", "page_count", "size_bytes", "created_at", "expires_at")
    readonly_fields = ("token_hash", "organization", "document", "filename", "size_bytes", "page_count", "ip_hash",
                       "created_at", "expires_at")
