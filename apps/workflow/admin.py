from django.contrib import admin

from .models import Assignment, AssignmentRules, Comment, Notification, Preference, VendorRule


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ("shipment", "organization", "assignee", "reason", "assigned_by", "assigned_at")
    list_filter = ("organization", "reason")
    raw_id_fields = ("shipment", "assignee", "assigned_by")


@admin.register(AssignmentRules)
class AssignmentRulesAdmin(admin.ModelAdmin):
    list_display = ("organization", "mode", "include_approvers", "vendor_fallback", "updated_at")


@admin.register(VendorRule)
class VendorRuleAdmin(admin.ModelAdmin):
    list_display = ("vendor_name", "organization", "assignee", "created_at")
    list_filter = ("organization",)


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    """Read only: comments are changed by their authors in the app, where every change is audited."""

    list_display = ("pk", "organization", "author", "shipment", "document", "created_at", "edited_at", "deleted_at")
    list_filter = ("organization",)
    readonly_fields = [f.name for f in Comment._meta.fields] + ["mentions"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "organization", "kind", "created_at", "read_at", "emailed_at")
    list_filter = ("organization", "kind")
    readonly_fields = [f.name for f in Notification._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(Preference)
class PreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "shortcuts", "email_mentions", "email_assignments", "updated_at")
