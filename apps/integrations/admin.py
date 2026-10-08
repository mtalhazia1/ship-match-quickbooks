from django.contrib import admin

from .models import WebhookDelivery, WebhookEndpoint, WebhookEvent


@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(admin.ModelAdmin):
    list_display = ("organization", "host", "enabled", "consecutive_failures", "last_success_at", "created_at")
    list_filter = ("enabled",)
    exclude = ("secret", "previous_secret", "url")
    readonly_fields = ("organization", "display_url", "events", "consecutive_failures", "disabled_at",
                       "disabled_reason", "last_success_at", "last_failure_at", "created_by")


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "type", "organization", "created_at")
    list_filter = ("type",)
    search_fields = ("event_id",)


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = ("event", "endpoint", "status", "attempts", "response_status", "created_at")
    list_filter = ("status",)
