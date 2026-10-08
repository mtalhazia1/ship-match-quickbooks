from django.contrib import admin

from .models import ContainerFreeTime, CustomsSettings, HtsReview


@admin.register(ContainerFreeTime)
class ContainerFreeTimeAdmin(admin.ModelAdmin):
    list_display = ("container_number", "shipment", "organization", "lfd_demurrage", "lfd_detention", "picked_up_on",
                    "returned_on")
    list_filter = ("organization",)
    search_fields = ("container_number", "shipment__reference")
    raw_id_fields = ("shipment", "document", "picked_up_by", "returned_by")


@admin.register(CustomsSettings)
class CustomsSettingsAdmin(admin.ModelAdmin):
    list_display = ("organization", "lfd_alert_days", "count_weekends")


@admin.register(HtsReview)
class HtsReviewAdmin(admin.ModelAdmin):
    list_display = ("hts_code", "description_key", "plausible", "organization", "created_at")
    list_filter = ("plausible", "organization")
