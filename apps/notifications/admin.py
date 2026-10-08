from django.contrib import admin

from .models import Channel, Delivery, NotificationSettings


@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "organization", "enabled", "updated_at")
    list_filter = ("kind", "enabled")
    exclude = ("webhook_url",)  # secret; edit it in Settings > Alerts


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "organization", "channel", "event", "status", "http_status", "attempts")
    list_filter = ("status", "event")
    readonly_fields = [f.name for f in Delivery._meta.fields]


admin.site.register(NotificationSettings)
