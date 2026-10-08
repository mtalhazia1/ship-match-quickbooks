from django.contrib import admin

from .models import ApprovedAccessorial, CaughtCharge, ChargeAlias, Quote, QuoteCharge, RateSettings


class QuoteChargeInline(admin.TabularInline):
    model = QuoteCharge
    extra = 0


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = ("vendor_name", "reference", "origin", "destination", "equipment", "valid_from", "valid_to",
                    "currency", "archived", "organization")
    list_filter = ("organization", "equipment", "archived")
    search_fields = ("vendor_name", "reference", "origin", "destination")
    inlines = [QuoteChargeInline]


@admin.register(ApprovedAccessorial)
class ApprovedAccessorialAdmin(admin.ModelAdmin):
    list_display = ("vendor_name", "code", "unit", "free_units", "max_per_unit", "max_amount", "currency",
                    "organization")
    list_filter = ("organization", "code")


@admin.register(ChargeAlias)
class ChargeAliasAdmin(admin.ModelAdmin):
    list_display = ("key", "code", "source", "organization", "updated_at")
    list_filter = ("organization", "source", "code")


@admin.register(CaughtCharge)
class CaughtChargeAdmin(admin.ModelAdmin):
    list_display = ("catch_key", "vendor_name", "currency", "amount_caught", "amount_latest", "first_caught_at",
                    "cleared_at", "organization")
    list_filter = ("organization", "code", "cleared_reason")
    readonly_fields = [f.name for f in CaughtCharge._meta.fields]


admin.site.register(RateSettings)
