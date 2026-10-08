from django.contrib import admin

from .models import BillingAccount, Onboarding, PendingSignup, StripeEvent


@admin.register(BillingAccount)
class BillingAccountAdmin(admin.ModelAdmin):
    list_display = ("organization", "plan", "status", "stripe_status", "trial_ends_at", "current_period_end")
    list_filter = ("status", "plan")
    search_fields = ("organization__name", "organization__slug", "stripe_customer_id", "stripe_subscription_id")
    readonly_fields = ("stripe_synced_at", "created_at", "updated_at")


@admin.register(StripeEvent)
class StripeEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "type", "organization", "outcome", "received_at")
    list_filter = ("type",)
    search_fields = ("event_id",)
    readonly_fields = [f.name for f in StripeEvent._meta.fields]


@admin.register(PendingSignup)
class PendingSignupAdmin(admin.ModelAdmin):
    list_display = ("email", "company_name", "created_at", "verified_at", "organization")
    search_fields = ("email", "company_name")
    exclude = ("password_hash", "nonce")
    readonly_fields = ("email", "company_name", "full_name", "ip_hash", "emails_sent", "created_at", "verified_at",
                       "organization", "user")


admin.site.register(Onboarding)
