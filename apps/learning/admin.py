from django.contrib import admin

from .models import DocumentLearning, VendorProfile


@admin.register(VendorProfile)
class VendorProfileAdmin(admin.ModelAdmin):
    list_display = ("display_name", "doc_type", "organization", "correction_count", "date_format", "last_learned_at")
    list_filter = ("doc_type", "date_format")
    search_fields = ("display_name", "vendor_key")


@admin.register(DocumentLearning)
class DocumentLearningAdmin(admin.ModelAdmin):
    list_display = ("document", "vendor_name", "corrections_at_read", "updated_at")
    raw_id_fields = ("document", "profile")
