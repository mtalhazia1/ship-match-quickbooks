from django.contrib import admin

from .models import PaymentSync, PostedBill, QBOConnection, VendorMapping, XeroConnection


@admin.register(QBOConnection)
class QBOConnectionAdmin(admin.ModelAdmin):
    list_display = ("organization", "realm_id", "access_expires_at", "default_expense_account_id", "connected_at")
    exclude = ("access_token", "refresh_token")


@admin.register(XeroConnection)
class XeroConnectionAdmin(admin.ModelAdmin):
    list_display = ("organization", "tenant_name", "tenant_id", "needs_reconnect", "default_account_code",
                    "bill_status", "connected_at")
    exclude = ("access_token", "refresh_token")


@admin.register(VendorMapping)
class VendorMappingAdmin(admin.ModelAdmin):
    list_display = ("organization", "display_name", "qbo_vendor_id", "expense_account_name", "expense_account_id",
                    "xero_contact_id", "xero_account_code", "learned_from_correction", "updated_at")
    list_filter = ("organization",)
    search_fields = ("display_name", "vendor_key")


@admin.register(PostedBill)
class PostedBillAdmin(admin.ModelAdmin):
    list_display = ("document", "shipment", "system", "status", "qbo_bill_id", "external_number", "payment_status",
                    "due_date", "paid_on", "posted_at")
    list_filter = ("system", "status", "payment_status", "organization")
    readonly_fields = ("response", "payments")


@admin.register(PaymentSync)
class PaymentSyncAdmin(admin.ModelAdmin):
    list_display = ("organization", "last_finished_at", "last_auto_at", "paused_until", "last_error")
