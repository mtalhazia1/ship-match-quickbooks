"""Audit log wording for landed cost and shared invoices (issue titles are in apps/shipments/labels.py)."""

ACTIONS = {
    "shared_invoice.detected": "found that invoice {invoice} covers {shipments} and split it {basis}",
    "shared_invoice.split_changed": "changed the split of invoice {invoice} ({basis})",
    "shared_invoice.confirmed": "confirmed the split of invoice {invoice}",
    "shared_invoice.share_confirmed": "confirmed this shipment's share of invoice {invoice} on {primary}: {currency} {share}",
    "shared_invoice.dismissed": "marked invoice {invoice} as not shared",
    "shared_invoice.reset": "went back to the automatic split of invoice {invoice}",
    "landed.method_changed": "changed how landed cost is spread on this shipment: {policy}",
    "landed.settings_updated": "changed how landed cost is spread: {policy}",
    "landed.exported": "exported landed cost ({what})",
}

AUDIT_GROUPS = [("shared_invoice.", "Shared invoices"), ("landed.", "Landed cost")]
