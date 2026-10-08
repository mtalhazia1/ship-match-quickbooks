from django.contrib import admin

from .models import AccrualAdjustment, AccrualSnapshot, CloseSettings, ReconItem, VendorPayment, VendorStatement


@admin.register(AccrualSnapshot)
class AccrualSnapshotAdmin(admin.ModelAdmin):
    """Locked versions are read-only everywhere, including the platform admin."""

    list_display = ("organization", "period_end", "version", "total", "currency", "locked_by", "locked_at")
    list_filter = ("organization",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(VendorStatement)
class VendorStatementAdmin(admin.ModelAdmin):
    list_display = ("organization", "vendor_name", "statement_date", "status", "created_at")
    list_filter = ("organization", "status")
    readonly_fields = ("sha256", "text", "summary", "notes", "llm_usage")


admin.site.register(CloseSettings)
admin.site.register(AccrualAdjustment)
admin.site.register(VendorPayment)
admin.site.register(ReconItem)
