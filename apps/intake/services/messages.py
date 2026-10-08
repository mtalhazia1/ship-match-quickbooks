"""Feedback after an upload, in words a bookkeeper understands."""
from __future__ import annotations

from django.contrib import messages

from .archives import summary


def _names(items: list[dict], limit: int = 3) -> str:
    names = [m["name"].replace("\\", "/").rsplit("/", 1)[-1] for m in items]
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def archive_messages(archives) -> list[tuple[int, str]]:
    out = []
    for archive in archives:
        s = summary(archive)
        added, dupes, skipped, ignored = s["added"], s["duplicate"], s["skipped"], s["ignored"]
        text = f"Unpacked {archive.original_filename}: {len(added)} document{'s' if len(added) != 1 else ''} added"
        text += f" ({_names(added)})." if added else "."
        if dupes:
            text += f" {len(dupes)} {'was' if len(dupes) == 1 else 'were'} received before ({_names(dupes)})."
        if ignored:
            text += f" {len(ignored)} system or hidden file{'s were' if len(ignored) != 1 else ' was'} ignored."
        out.append((messages.SUCCESS if added else messages.INFO, text))
        if skipped:
            reasons = "; ".join(f"{_names([m], 1)}: {m['reason']}" for m in skipped[:5])
            more = f" and {len(skipped) - 5} more" if len(skipped) > 5 else ""
            out.append((messages.WARNING, f"Not added from {archive.original_filename}: {reasons}{more}. "
                                          "Send these as PDF, image or spreadsheet files."))
    return out


def photo_hint(docs) -> str:
    """Photos are read by OCR. Without it a reviewer types the key numbers, so say so up front."""
    from apps.documents.services.ocr import ocr_provider

    photos = [d for d in docs if d.source_format == "image"]
    if not photos or ocr_provider() != "text":
        return ""
    names = ", ".join(d.original_filename for d in photos[:3])
    return (f"{names} {'is a photo' if len(photos) == 1 else 'are photos'}. Text recognition (OCR) is off, so type the "
            "B/L, container or PO number on the document's page to match it. Ask your administrator to turn on "
            "reading of photos and scans.")
