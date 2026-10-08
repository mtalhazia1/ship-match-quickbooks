"""Template helpers for intake: the upload accept list and short descriptions of where a document came from."""
from django import template

from apps.intake.services.formats import ACCEPT, SUPPORTED_TEXT, human_size

register = template.Library()


@register.simple_tag
def accept_types() -> str:
    return ACCEPT


@register.simple_tag
def supported_types() -> str:
    return SUPPORTED_TEXT


@register.filter
def pages_label(pages) -> str:
    """[3, 4] -> 'pages 3-4', [2, 2] -> 'page 2'."""
    if not pages:
        return ""
    first, last = pages[0], pages[-1]
    return f"page {first}" if first == last else f"pages {first}-{last}"


@register.filter
def kb(size) -> str:
    return human_size(size)
