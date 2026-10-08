"""Template helpers for the interface: icons, durations, money, initials."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

# 24x24 stroke icons (drawn for ShipMatch).
_ICONS = {
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20.5 20.5 16 16"/>',
    "inbox": '<path d="M3 13h5l1.5 3h5l1.5-3h5"/><path d="M5.5 5h13L21 13v6H3v-6z"/>',
    "file": '<path d="M14 3H6v18h12V7z"/><path d="M14 3v4h4"/>',
    "grid": '<rect x="3.5" y="3.5" width="7" height="7" rx="1"/><rect x="13.5" y="3.5" width="7" height="7" rx="1"/>'
            '<rect x="3.5" y="13.5" width="7" height="7" rx="1"/><rect x="13.5" y="13.5" width="7" height="7" rx="1"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
    "users": '<circle cx="9" cy="8" r="3.5"/><path d="M3 20c0-3.6 2.7-6 6-6s6 2.4 6 6"/>'
             '<path d="M16 4.6a3.5 3.5 0 0 1 0 6.8"/><path d="M18 14.3c1.8.7 3 2.8 3 5.7"/>',
    "sliders": '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/>'
               '<circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    "logout": '<path d="M15 4h4v16h-4"/><path d="M10 8l-4 4 4 4"/><path d="M6 12h10"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7"/>',
    "x": '<path d="M6 6l12 12M18 6 6 18"/>',
    "alert": '<path d="M12 4l9 16H3z"/><path d="M12 10v4"/><path d="M12 17.2v.1"/>',
    "alert-circle": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5V13"/><path d="M12 16.3v.1"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6"/><path d="M12 7.7v.1"/>',
    "upload": '<path d="M12 16V4"/><path d="M7 9l5-5 5 5"/><path d="M4 16v4h16v-4"/>',
    "external": '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v6H4V6h6"/>',
    "chevron-right": '<path d="M9 6l6 6-6 6"/>',
    "arrow-left": '<path d="M19 12H5"/><path d="M11 6l-6 6 6 6"/>',
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9"/><path d="M17 6l3 3"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    "download": '<path d="M12 4v12"/><path d="M7 11l5 5 5-5"/><path d="M4 20h16"/>',
    "eye": '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    "book": '<path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h11"/>',
    "send": '<path d="M4 12l16-8-6 16-3-7z"/>',
    "container": '<rect x="2.5" y="6" width="19" height="12" rx="1"/><path d="M6.5 8.5v7M10 8.5v7M13.5 8.5v7M17 8.5v7"/>',
    "tag": '<path d="M3.5 12.5V4.5a1 1 0 0 1 1-1h8l8 8-9 9z"/><circle cx="8.5" cy="8.5" r="1.5"/>',
    "trending": '<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
}


@register.simple_tag
def icon(name: str, label: str = ""):
    body = _ICONS.get(name, _ICONS["info"])
    if label:
        return format_html('<svg class="i" viewBox="0 0 24 24" role="img" aria-label="{}">{}</svg>', label, mark_safe(body))
    return mark_safe(f'<svg class="i" viewBox="0 0 24 24" aria-hidden="true">{body}</svg>')


@register.filter
def duration(hours):
    """3.5 -> '3.5 h', 0.2 -> '12 min', 50 -> '2.1 days'."""
    if hours is None or hours == "":
        return "–"
    h = float(hours)
    if h < 1:
        return f"{max(1, round(h * 60))} min"
    if h < 48:
        return f"{h:.1f} h".replace(".0 h", " h")
    return f"{h / 24:.1f} days"


@register.filter
def money(amount, currency=""):
    if amount in (None, ""):
        return "–"
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return str(amount)
    return f"{currency} {value:,.2f}".strip()


@register.filter
def initials(user):
    name = (user.get_full_name() or user.get_username() or "?").strip()
    parts = [p for p in name.replace("@", " ").replace(".", " ").split() if p]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


@register.filter
def display_name(user):
    if not user:
        return "ShipMatch"
    return user.get_full_name() or user.get_username()


@register.filter
def get(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None
