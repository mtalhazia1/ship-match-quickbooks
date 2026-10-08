"""Template tags for evidence highlighting (see apps/shipments/services/evidence.py)."""
from django import template
from django.utils.html import json_script

from apps.shipments.services.evidence import doc_payload, issue_targets

register = template.Library()


@register.simple_tag
def evidence_data(doc):
    """Where the document's values are printed, as a JSON script tag (not executed; CSP-safe)."""
    return json_script(doc_payload(doc), f"evidence-doc-{doc.pk}")


@register.inclusion_tag("evidence/_issue_button.html")
def evidence_issue_button(issue):
    """A "Show on page" button for an issue tied to values on a document (hidden until the viewer is ready)."""
    found = issue_targets(issue)
    if not found:
        return {"show": False}
    targets, description = found
    return {"show": True, "doc_id": issue.document_id, "targets": " ".join(targets), "description": description}
