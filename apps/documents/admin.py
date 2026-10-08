from django.contrib import admin

from .models import Document, ExtractedField, IngestedEmail


class FieldInline(admin.TabularInline):
    model = ExtractedField
    extra = 0
    fields = ("name", "value", "confidence", "grounded", "source")


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "organization", "doc_type", "status", "source", "received_at")
    list_filter = ("organization", "doc_type", "status", "source")
    search_fields = ("original_filename", "sha256")
    readonly_fields = ("sha256", "text", "received_at", "updated_at")
    inlines = [FieldInline]


@admin.register(IngestedEmail)
class IngestedEmailAdmin(admin.ModelAdmin):
    list_display = ("subject", "sender", "organization", "received_at", "attachment_count")
    list_filter = ("organization",)
