"""Audit log wording for exports and webhooks. Added to apps.shipments.labels.ACTIONS at start-up."""

ACTIONS = {
    "export.downloaded": "exported {kind} ({format})",
    "webhook.created": "added a webhook endpoint for {host}",
    "webhook.updated": "changed the webhook endpoint for {host}",
    "webhook.deleted": "removed the webhook endpoint for {host}",
    "webhook.secret_rotated": "created a new signing secret for the webhook to {host}",
    "webhook.tested": "sent a test event to {host} ({result})",
    "webhook.replayed": "sent {type} to {host} again",
    "webhook.disabled": "turned off the webhook to {host}: {reason}",
}

AUDIT_GROUPS = [("export.", "Exports"), ("webhook.", "Webhooks")]
