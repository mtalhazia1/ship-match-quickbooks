"""Audit log wording for alert settings. Added to apps.shipments.labels.ACTIONS at start-up."""

ACTIONS = {
    "notification_channel.created": "added the alert channel {name} ({kind})",
    "notification_channel.updated": "changed the alert channel {name}",
    "notification_channel.deleted": "removed the alert channel {name}",
    "notification_channel.tested": "sent a test alert to {name} ({status})",
    "notification_settings.updated": "set the daily summary time to {hour}",
    "qbo.needs_reconnect": "found that QuickBooks needs to be connected again",
}

AUDIT_GROUPS = [("notification", "Alerts")]
