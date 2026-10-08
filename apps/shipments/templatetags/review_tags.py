from django import template

register = template.Library()


@register.filter
def show(value):
    """Render an extracted value for humans: lists joined, None as a dash."""
    if value in (None, "", []):
        return "—"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


@register.filter
def doc_error(value):
    """A document's stored processing error in plain language, never a traceback or a server path."""
    from apps.documents.services.errors import display

    return display(value)


@register.filter
def as_input(value):
    if value in (None, []):
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


@register.filter
def label(name):
    return str(name).replace("_", " ").capitalize().replace("Bl ", "B/L ").replace("Po ", "PO ")


@register.filter
def conf_class(field):
    if field is None:
        return "missing"
    if field.source == "human":
        return "human"
    return "ok" if field.confidence >= 0.85 else "low"
