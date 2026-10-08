from django.apps import AppConfig

# Icons this app uses, added to the shared icon set at start-up (24x24 strokes, like the others).
ICONS = {
    "bell": '<path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    "keyboard": '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.01M9.5 10h.01M13 10h.01M16.5 10h.01'
                'M7 14h10"/>',
    "message": '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9.5h8M8 12.5h5"/>',
    "user-check": '<circle cx="9" cy="8" r="3.5"/><path d="M3 20c0-3.6 2.7-6 6-6 1.5 0 2.9.5 3.9 1.4"/>'
                  '<path d="M15 18l2 2 4-4.5"/>',
    "briefcase": '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M9 7V5h6v2"/><path d="M3 12.5h18"/>',
    "chevron-left": '<path d="M15 6l-6 6 6 6"/>',
}


class WorkflowConfig(AppConfig):
    name = "apps.workflow"
    label = "workflow"
    verbose_name = "Team workflow"

    def ready(self):
        from django.db.models.signals import post_save, pre_save

        from apps.core import context_processors, views
        from apps.core.templatetags import ui
        from apps.demo import middleware as demo_middleware
        from apps.shipments import labels
        from apps.shipments.models import Shipment
        from apps.shipments.services.approval import register_approval_blocker

        from . import labels as workflow_labels
        from .services import assignment, decisions, notify

        # Registered here instead of edited into shared files, to keep feature branches merge-friendly.
        for action, text in workflow_labels.ACTIONS.items():
            labels.ACTIONS.setdefault(action, text)
        for group in workflow_labels.AUDIT_GROUPS:
            if group not in views.AUDIT_ACTION_GROUPS:
                views.AUDIT_ACTION_GROUPS.append(group)
        for name, icon in ICONS.items():
            ui._ICONS.setdefault(name, icon)
        sections = {
            "workflow:portfolio": "clients", "workflow:my_work": "my_work", "workflow:quick": "queue",
            "workflow:approval_link": "queue", "workflow:bulk": "queue", "workflow:batch": "queue",
            "workflow:notifications": "notifications", "workflow:shortcuts": "account",
            "workflow:assignment_settings": "settings",
        }
        for key, section in sections.items():
            context_processors.SECTIONS.setdefault(key, section)

        # Approving from another organization (firm lists, alert links) checks that organization's
        # two-factor rule too; the shipment page and bulk actions get the same rule this way.
        register_approval_blocker(decisions.mfa_blocker)
        notify.register_alerts()

        # The shared demo accounts must not create organizations that the nightly reset never removes.
        demo_middleware.RULES.setdefault("workflow:create_org", (
            "demo", None, "creating client organizations is turned off.", "workflow:portfolio"))

        pre_save.connect(assignment.before_shipment_save, sender=Shipment, dispatch_uid="workflow.shipment_pre")
        post_save.connect(assignment.on_shipment_saved, sender=Shipment, dispatch_uid="workflow.shipment_post")
