"""Learn from a reviewer's correction: the label the vendor prints, its date order, and the mistake."""
from __future__ import annotations

import logging
from datetime import date

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounting.models import vendor_key
from apps.core.utils import audit
from apps.documents.models import Document
from apps.learning.models import VendorProfile

from . import dates
from .identify import vendor_field
from .labels import DATE_FIELDS, LEARNABLE, label_before

log = logging.getLogger(__name__)
MAX_EXAMPLES = 10


def _as_text(value) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def learn_from_correction(doc: Document, name: str, old, new, user=None) -> VendorProfile | None:
    """Update the vendor's profile after a reviewer changed `name` from `old` to `new`.

    Never raises: a learning problem must not block the correction itself.
    """
    if not settings.VENDOR_LEARNING:
        return None
    try:
        with transaction.atomic():
            return _learn(doc, name, old, new, user)
    except Exception:  # noqa: BLE001 - logged; the correction is already saved
        log.exception("Vendor learning failed for document %s field %s", doc.pk, name)
        return None


def _learn(doc: Document, name: str, old, new, user) -> VendorProfile | None:
    vfield = vendor_field(doc.doc_type)
    if vfield is None:
        return None
    vendor = new if name == vfield else doc.field(vfield)
    key = vendor_key(vendor or "")
    if not key:
        return None  # we don't know who sent it yet; nothing to attach the lesson to
    profile, _ = (VendorProfile.objects.select_for_update()
                  .get_or_create(organization=doc.organization, vendor_key=key, doc_type=doc.doc_type,
                                 defaults={"display_name": str(vendor)[:200]}))
    now = timezone.now()
    learned: dict = {}

    profile.correction_count += 1
    profile.field_corrections[name] = profile.field_corrections.get(name, 0) + 1
    profile.examples = (profile.examples + [{
        "field": name, "old": _as_text(old)[:120], "new": _as_text(new)[:120], "document": doc.pk,
        "at": now.isoformat(timespec="seconds"),
    }])[-MAX_EXAMPLES:]
    if name == vfield:
        profile.display_name = str(new)[:200]

    if name in LEARNABLE and new not in (None, "", []) and name != vfield:
        label = label_before(doc.text, name, new)
        if label:
            current = profile.labels.get(name) or {}
            same = current.get("label", "").lower() == label.lower()
            profile.labels[name] = {"label": label, "hits": (current.get("hits", 0) + 1) if same else 1,
                                    "learned_at": now.isoformat(timespec="seconds")}
            if not same:
                learned["label"] = label

    if name in DATE_FIELDS and new:
        try:
            corrected = date.fromisoformat(str(new))
        except ValueError:
            corrected = None
        order = dates.vote(doc.text, corrected) if corrected else ""
        if order:
            profile.date_votes[order] = profile.date_votes.get(order, 0) + 1
            votes = profile.date_votes
            winner = dates.DMY if votes.get(dates.DMY, 0) > votes.get(dates.MDY, 0) else (
                dates.MDY if votes.get(dates.MDY, 0) > votes.get(dates.DMY, 0) else "")
            if winner != profile.date_format:
                profile.date_format = winner
                learned["date_format"] = winner or "unsure"

    profile.last_learned_at = now
    profile.save()
    if learned:
        audit(doc.organization, "learning.updated", profile, actor=user, vendor=profile.display_name,
              doc_type=doc.doc_type, field=name, document=doc.pk, **learned)
    return profile
