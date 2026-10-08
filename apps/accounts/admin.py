from django.contrib import admin

from .models import ApiKey, Profile


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "timezone", "two_factor")
    search_fields = ("user__username", "user__email")
    fields = ("user", "timezone", "mfa_enabled_at")
    readonly_fields = ("user", "mfa_enabled_at")

    @admin.display(boolean=True, description="Two-factor")
    def two_factor(self, obj):
        return obj.mfa_enabled

    def has_add_permission(self, request):
        return False


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "prefix", "role", "created_by", "created_at", "last_used_at", "revoked_at")
    list_filter = ("organization", "role")
    search_fields = ("name", "prefix")
    readonly_fields = [f.name for f in ApiKey._meta.fields]

    def has_add_permission(self, request):
        return False  # keys are created in Settings > API keys, where the secret is shown once
