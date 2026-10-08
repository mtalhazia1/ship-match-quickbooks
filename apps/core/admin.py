from django.contrib import admin

from .models import AuditEvent, Membership, Organization
from .utils import audit

# The platform admin manages the platform: organizations, who belongs to them, billing and sign-ups. It never
# edits a client's business data (documents, shipments, bills, disputes, rates ...): those models are read only
# here, and changing them goes through the app as a member, under the organization's roles and controls.
EDITABLE_IN_PLATFORM_ADMIN = {
    "auth.user", "auth.group",
    "core.organization", "core.membership",
    "billing.billingaccount", "billing.pendingsignup",
}

# A client's own controls: set when an operator creates the organization, then changed only by its admins.
ORG_CONTROLS = ("home_currency", "timezone", "require_mfa", "maker_checker", "review_threshold", "fx_rates")


def _no(*args, **kwargs):
    return False


def lock_client_data(site=admin.site) -> None:
    """Make every registered model outside EDITABLE_IN_PLATFORM_ADMIN view-only (no add, change or delete)."""
    for model, model_admin in site._registry.items():
        if model._meta.label_lower in EDITABLE_IN_PLATFORM_ADMIN:
            continue
        model_admin.has_add_permission = _no
        model_admin.has_change_permission = _no
        model_admin.has_delete_permission = _no


def _audit_membership(request, membership: Membership, action: str) -> None:
    audit(membership.organization, f"team.platform_{action}", membership, actor=request.user, via="platform admin",
          user=membership.user.get_username(), role=membership.role)


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    fields = ("user", "role", "approval_limit")
    autocomplete_fields = ("user",)


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "home_currency", "timezone", "require_mfa", "maker_checker", "created_at")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "slug")
    inlines = [MembershipInline]

    def get_readonly_fields(self, request, obj=None):
        return ORG_CONTROLS if obj else ()

    def has_delete_permission(self, request, obj=None):
        return False   # deleting a client's organization would delete all its records

    def save_formset(self, request, form, formset, change):
        if formset.model is not Membership:
            return super().save_formset(request, form, formset, change)
        instances = formset.save(commit=False)
        for m in formset.deleted_objects:
            _audit_membership(request, m, "removed")
            m.delete()
        for m in instances:
            new = m.pk is None
            m.save()
            _audit_membership(request, m, "added" if new else "updated")
        formset.save_m2m()


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "role", "approval_limit", "created_at")
    list_filter = ("organization", "role")
    search_fields = ("user__username", "user__email", "organization__name")
    autocomplete_fields = ("user",)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        _audit_membership(request, obj, "updated" if change else "added")

    def delete_model(self, request, obj):
        _audit_membership(request, obj, "removed")
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for m in queryset.select_related("organization", "user"):
            _audit_membership(request, m, "removed")
        super().delete_queryset(request, queryset)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "organization", "actor", "action", "object_type", "object_id", "ip")
    list_filter = ("organization", "action")
    search_fields = ("object_id", "action", "request_id")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in AuditEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
