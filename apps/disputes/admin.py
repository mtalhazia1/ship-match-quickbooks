from django.contrib import admin

from .models import Dispute, DisputeEvent, DisputeItem, DisputeSettings, VendorContact


class ItemInline(admin.TabularInline):
    model = DisputeItem
    extra = 0
    readonly_fields = ("issue", "code", "title", "amount", "currency")
    fields = readonly_fields


class EventInline(admin.TabularInline):
    model = DisputeEvent
    extra = 0
    readonly_fields = ("kind", "text", "actor", "created_at")
    fields = readonly_fields


@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ("reference", "organization", "vendor_name", "status", "amount_disputed", "amount_recovered",
                    "currency", "sent_at", "follow_up_on")
    list_filter = ("status", "organization")
    search_fields = ("reference", "vendor_name", "invoice_number", "shipment_reference")
    raw_id_fields = ("shipment", "invoice", "credit_note")
    readonly_fields = ("message_id", "created_at", "updated_at")
    inlines = [ItemInline, EventInline]


@admin.register(VendorContact)
class VendorContactAdmin(admin.ModelAdmin):
    list_display = ("vendor_name", "email", "organization", "updated_at")
    search_fields = ("vendor_name", "email")


@admin.register(DisputeSettings)
class DisputeSettingsAdmin(admin.ModelAdmin):
    list_display = ("organization", "reply_to", "follow_up_days")
