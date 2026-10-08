"""Which known vendor sent this document? Cheap fuzzy matching of the top of the text against the
names of vendors this organization has taught ShipMatch about (no AI call)."""
from __future__ import annotations

from django.conf import settings
from rapidfuzz import fuzz

from apps.accounting.models import vendor_key
from apps.learning.models import VendorProfile

HEAD_LINES = 25
VENDOR_FIELDS = {"commercial_invoice": "vendor_name", "freight_invoice": "vendor_name", "bill_of_lading": "carrier_name"}


def vendor_field(doc_type: str) -> str | None:
    return VENDOR_FIELDS.get(doc_type)


def _line_score(name_key: str, line_key: str) -> float:
    if not name_key or not line_key:
        return 0.0
    if name_key == line_key:
        return 100.0
    # A line shorter than the name can't contain it ("Metro" must not match "metro drayage").
    if len(line_key) < len(name_key) - 2:
        return fuzz.ratio(name_key, line_key)
    return fuzz.partial_ratio(name_key, line_key)


def identify(org, doc_type: str, text: str, extracted_name: str | None = None) -> tuple[VendorProfile | None, float]:
    """Best matching vendor profile for this document and its score (0-100)."""
    profiles = list(VendorProfile.objects.filter(organization=org, doc_type=doc_type))
    if not profiles:
        return None, 0.0
    threshold = settings.LEARNING_MATCH_SCORE
    if extracted_name:
        key = vendor_key(extracted_name)
        exact = next((p for p in profiles if p.vendor_key == key), None)
        if exact:
            return exact, 100.0
    head = [vendor_key(ln) for ln in (text or "").splitlines()[:HEAD_LINES]]
    head = [h for h in head if len(h) >= 3]
    best, best_score = None, 0.0
    for p in profiles:
        if len(p.vendor_key) < 4:
            continue
        score = max((_line_score(p.vendor_key, h) for h in head), default=0.0)
        if extracted_name:
            score = max(score, fuzz.ratio(p.vendor_key, vendor_key(extracted_name)))
        if score > best_score:
            best, best_score = p, score
    return (best, best_score) if best and best_score >= threshold else (None, best_score)
