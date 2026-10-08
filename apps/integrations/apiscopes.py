"""API key scopes: what each key may do, on top of its access level (read only, or read and upload).

Keys created before scopes existed have an empty list and keep working exactly as before: every read scope,
plus documents:write when the key has upload access.
"""
from __future__ import annotations

SCOPES = [
    ("shipments:read", "Shipments", "List shipments and read one with its documents, totals and issues"),
    ("documents:read", "Documents", "List documents and read the values read from each"),
    ("exports:read", "Exports", "Download the shipment, document and issue exports as CSV or Excel"),
    ("documents:write", "Upload", "Upload documents (needs read and upload access)"),
]
READ_SCOPES = ["shipments:read", "documents:read", "exports:read"]
WRITE_SCOPE = "documents:write"
LABELS = {key: label for key, label, _ in SCOPES}


def scopes_of(key) -> list[str]:
    explicit = [s for s in (getattr(key, "scopes", None) or []) if s in LABELS]
    if explicit:
        return explicit
    out = list(READ_SCOPES)
    if getattr(key, "role", "") == "reviewer":
        out.append(WRITE_SCOPE)
    return out


def allows(key, scope: str) -> bool:
    return scope in scopes_of(key)


def scopes_from_post(post, role: str) -> list[str] | None:
    """Scopes ticked on the "Create a key" form; [] = everything the access level allows (a form without the
    scope choices); None when nothing usable was chosen."""
    if not post.get("scopes_present"):
        return []
    chosen = [s for s in post.getlist("scopes") if s in READ_SCOPES]
    if role == "reviewer":
        chosen.append(WRITE_SCOPE)
    return chosen or None


def labels(key) -> list[str]:
    return [LABELS[s] for s in scopes_of(key)]
