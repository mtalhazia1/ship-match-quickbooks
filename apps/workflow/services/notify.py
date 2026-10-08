"""Personal notifications: the bell in the top bar, and an email to the person when they want one.

Team channels (Slack, Teams, shared email) get the same events through apps.notifications: see
register_alerts() and the builders below. Everything here is best effort: a notification that can't be
created or sent never breaks the action that caused it.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Membership

from ..models import Notification, Preference

log = logging.getLogger(__name__)

ASSIGNED_EVENT = "shipment.assigned_to_you"
MENTION_EVENT = "comment.mention"


def display(user) -> str:
    if user is None:
        return "ShipMatch"
    return user.get_full_name() or user.get_username()


def with_org(path: str, org) -> str:
    """Add ?org=<slug> so opening the link switches to the right organization (the normal org switch)."""
    if not path:
        return path
    base, _, fragment = path.partition("#")
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}org={org.slug}" + (f"#{fragment}" if fragment else "")


def notify_user(user, org, kind: str, title: str, *, body: str = "", url: str = "", actor=None, obj=None
                ) -> Notification | None:
    """One notification for one person, only while they belong to the organization."""
    try:
        if user is None or not user.is_active or (actor is not None and user.pk == actor.pk):
            return None
        if not Membership.objects.filter(user=user, organization=org).exists():
            return None
        n = Notification.objects.create(
            user=user, organization=org, kind=kind, title=title[:250], body=(body or "")[:500], url=url[:500],
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            object_type=type(obj).__name__ if obj is not None else "", object_id=str(getattr(obj, "pk", "") or ""))
    except Exception:
        log.exception("Could not create a %s notification", kind)
        return None
    prefs = Preference.for_user(user)
    wants = prefs.email_assignments if kind == Notification.Kind.ASSIGNED else prefs.email_mentions
    if wants and user.email:
        pk = n.pk

        def queue():
            from ..tasks import email_notification

            try:
                email_notification.delay(pk)
            except Exception:
                log.exception("Could not queue the email for notification %s", pk)

        transaction.on_commit(queue)
    return n


def send_email(notification_id: int) -> bool:
    """Email one notification to its person. Never sends from a public demo."""
    from apps.demo.mail import outbound_blocked
    from apps.notifications.events import absolute

    n = (Notification.objects.select_related("user", "organization", "actor").filter(pk=notification_id).first())
    if n is None or n.emailed_at is not None or not n.user.email or not n.user.is_active:
        return False
    if outbound_blocked():
        log.info("Demo mode: notification email %s not sent", n.pk)
        return False
    link = absolute(with_org(n.url, n.organization)) if n.url else absolute(reverse("workflow:notifications"))
    lines = [f"Hello {n.user.get_short_name() or n.user.get_username()},", "", n.title + "."]
    if n.body:
        lines += ["", f"“{n.body}”"]
    lines += ["", f"Open it in ShipMatch: {link}", "",
              f"Sent by ShipMatch for {n.organization.name}. Change which emails you get under "
              f"{absolute(reverse('workflow:shortcuts'))}"]
    claimed = Notification.objects.filter(pk=n.pk, emailed_at__isnull=True).update(emailed_at=timezone.now())
    if not claimed:
        return False
    try:
        send_mail(f"[ShipMatch] {n.title}"[:200].replace("\n", " "), "\n".join(lines),
                  settings.DEFAULT_FROM_EMAIL, [n.user.email], fail_silently=False)
    except Exception:
        log.exception("Could not email notification %s", n.pk)
        Notification.objects.filter(pk=n.pk).update(emailed_at=None)
        return False
    return True


def unread_count(user) -> int:
    if not getattr(user, "is_authenticated", False):
        return 0
    return visible(user).filter(read_at__isnull=True).count()


def visible(user):
    """A person's notifications from the organizations they still belong to."""
    return Notification.objects.filter(user=user, organization__memberships__user=user).distinct()


# --------------------------------------------------------------------------- team channel alerts


def register_alerts() -> None:
    from apps.notifications import events

    events.register_event(
        ASSIGNED_EVENT, "Assigned to you",
        "A shipment was assigned to a team member, by a person or by the assignment rules. The message names who.",
        audit_actions=["shipment.assigned"], builder=assigned_message, default_for=("slack", "teams"))
    events.register_event(
        MENTION_EVENT, "Mentioned in a comment",
        "Someone mentioned a team member with @name in a comment on a shipment or document.",
        audit_actions=["comment.created", "comment.edited"], builder=mention_message, default_for=())


def assigned_message(e):
    from django.contrib.auth import get_user_model

    from apps.notifications.events import Message, absolute
    from apps.shipments.models import Shipment

    shipment = Shipment.objects.filter(pk=e.object_id, organization=e.organization).first()
    assignee = get_user_model().objects.filter(pk=(e.data or {}).get("assignee_id")).first()
    if shipment is None or assignee is None:
        return None
    by = display(e.actor) if e.actor else "The assignment rules"
    facts = [["Assigned to", display(assignee)], ["Assigned by", by],
             ["Status", shipment.get_status_display()], ["Bill of lading", shipment.bl_number or "Not received"]]
    return Message(ASSIGNED_EVENT, f"{shipment.reference} assigned to {display(assignee)}",
                   f"{display(assignee)}, {shipment.reference} is yours to work on.", facts,
                   absolute(reverse("review:shipment", args=[shipment.pk])), "Open the shipment", "info",
                   e.organization.name)


def mention_message(e):
    from apps.notifications.events import Message, absolute

    from ..models import Comment

    names = (e.data or {}).get("mentioned_names") or []
    if not names:
        return None
    c = Comment.objects.filter(pk=e.object_id, organization=e.organization, deleted_at__isnull=True
                               ).select_related("shipment", "document").first()
    if c is None:
        return None
    where = target_label(c)
    text = (c.body or "").strip()
    text = text if len(text) <= 300 else text[:299] + "…"
    return Message(MENTION_EVENT, f"{display(e.actor)} mentioned {', '.join(names)} on {where}", text,
                   [["Where", where]], absolute(comment_path(c)), "Open the comment", "info", e.organization.name)


def target_label(c) -> str:
    if c.document_id:
        doc = c.document
        shipment = doc.match.shipment if hasattr(doc, "match") else None
        return f"{doc.original_filename}" + (f" in {shipment.reference}" if shipment else "")
    return c.shipment.reference if c.shipment_id else "a comment"


def comment_path(c) -> str:
    if c.document_id:
        doc = c.document
        if hasattr(doc, "match"):
            return reverse("review:shipment", args=[doc.match.shipment_id]) + f"#comment-{c.pk}"
        return reverse("review:document", args=[doc.pk]) + f"#comment-{c.pk}"
    return reverse("review:shipment", args=[c.shipment_id]) + f"#comment-{c.pk}"
