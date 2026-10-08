from django.contrib import admin

from .models import Approval, MatchLink, Shipment, ValidationIssue


class LinkInline(admin.TabularInline):
    model = MatchLink
    extra = 0
    readonly_fields = ("document", "method", "score", "reason", "created_at")


class IssueInline(admin.TabularInline):
    model = ValidationIssue
    extra = 0
    fields = ("code", "severity", "message", "resolved")
    readonly_fields = ("code", "severity", "message")


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ("reference", "organization", "bl_number", "status", "updated_at")
    list_filter = ("organization", "status")
    search_fields = ("reference", "bl_number")
    inlines = [LinkInline, IssueInline]


@admin.register(ValidationIssue)
class ValidationIssueAdmin(admin.ModelAdmin):
    list_display = ("code", "severity", "shipment", "document", "resolved", "created_at")
    list_filter = ("organization", "code", "severity", "resolved")


admin.site.register(Approval)
