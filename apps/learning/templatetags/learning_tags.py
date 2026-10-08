from django import template

from apps.accounting.models import vendor_key
from apps.learning.models import DocumentLearning, VendorProfile
from apps.learning.services.identify import vendor_field

register = template.Library()

HOW = {
    "label": "read after the label “{label}”",
    "date_format": "read day first",
    "vendor": "vendor name as corrected before",
    "hint": "read with this vendor's notes",
}


def field_label(name: str) -> str:
    return name.replace("_", " ").capitalize().replace("Bl ", "B/L ").replace("Po ", "PO ")


@register.simple_tag
def vendor_learning(doc):
    """What learning knows and did for this document, or None. Used by learning/_doc_note.html."""
    if doc is None or vendor_field(doc.doc_type) is None:
        return None
    record = DocumentLearning.objects.filter(document=doc).select_related("profile").first()
    profile = record.profile if record and record.profile_id else None
    if profile is None:
        name = doc.field(vendor_field(doc.doc_type))
        if name:
            profile = VendorProfile.objects.filter(organization_id=doc.organization_id, doc_type=doc.doc_type,
                                                   vendor_key=vendor_key(name)).first()
    filled = [{"name": field_label(n), "how": HOW.get(m.get("how"), "").format(label=m.get("label", "")),
               "replaced": m.get("replaced", "")}
              for n, m in ((record.fields or {}).items() if record else [])]
    if profile is None and not filled:
        return None
    if profile is not None and not profile.correction_count and not filled:
        return None
    return {"profile": profile, "corrections": profile.correction_count if profile else 0,
            "vendor": profile.display_name if profile else (record.vendor_name if record else ""), "filled": filled}
