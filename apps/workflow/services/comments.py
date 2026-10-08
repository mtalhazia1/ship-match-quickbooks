"""Comments on shipments and documents, with @mentions of the organization's members.

* A mention is @username. Only members of the comment's organization are recognised; anything else stays
  plain text and nobody outside the organization is ever notified.
* Replies hang under a top-level comment (one level, like most review tools).
* Authors may edit or delete their own comment for COMMENT_EDIT_MINUTES (default 15). Every create, edit
  and delete is in the audit log, with the previous text for edits and deletes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.html import conditional_escape, linebreaks
from django.utils.safestring import mark_safe

from apps.core.permissions import PERMISSION_TEXT, has_perm
from apps.core.utils import audit

from ..models import Comment, Notification
from .notify import comment_path, display, notify_user, target_label

BODY_MAX = 4000
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_][A-Za-z0-9_.+\-@]*)")
TRAILING = ".,;:!?)-"


class CommentError(ValueError):
    pass


def edit_window() -> timedelta:
    return timedelta(minutes=max(0, int(getattr(settings, "COMMENT_EDIT_MINUTES", 15))))


def members_by_username(org) -> dict:
    users = get_user_model().objects.filter(memberships__organization=org, is_active=True).distinct()
    return {u.get_username().lower(): u for u in users}


def _token(raw: str) -> str:
    return raw.rstrip(TRAILING)


def parse_mentions(org, body: str) -> list:
    """Members of org named with @username in body, in order, each once."""
    members = members_by_username(org)
    found = []
    for m in MENTION_RE.finditer(body or ""):
        user = members.get(_token(m.group(1)).lower())
        if user is not None and user not in found:
            found.append(user)
    return found


def unknown_mentions(org, body: str) -> list[str]:
    """@names in body that are not members of org, so nobody was notified for them."""
    members = members_by_username(org)
    out: list[str] = []
    for m in MENTION_RE.finditer(body or ""):
        name = _token(m.group(1))
        if name and name.lower() not in members and name not in out:
            out.append(name)
    return out


def render(comment: Comment) -> str:
    """Comment text as safe HTML: escaped, line breaks kept, recognised mentions highlighted."""
    if comment.is_deleted:
        return ""
    known = {u.get_username().lower(): u for u in comment.mentions.all()}
    out, pos, body = [], 0, comment.body or ""
    for m in MENTION_RE.finditer(body):
        token = _token(m.group(1))
        user = known.get(token.lower())
        if user is None:
            continue
        start, end = m.start(), m.start() + 1 + len(token)
        out.append(conditional_escape(body[pos:start]))
        out.append(f'<span class="wf-mention" title="{conditional_escape(user.get_username())}">'
                   f"@{conditional_escape(display(user))}</span>")
        pos = end
    out.append(conditional_escape(body[pos:]))
    # linebreaks() escapes nothing itself when given safe text; every piece above is already escaped.
    return mark_safe(linebreaks(mark_safe("".join(str(p) for p in out))))


def can_change(comment: Comment, user, now=None) -> bool:
    if comment.is_deleted or comment.author_id is None or comment.author_id != getattr(user, "pk", None):
        return False
    return (now or timezone.now()) - comment.created_at <= edit_window()


def minutes_left(comment: Comment, now=None) -> int:
    left = comment.created_at + edit_window() - (now or timezone.now())
    return max(0, int(left.total_seconds() // 60) + 1)


def _clean(body: str) -> str:
    body = (body or "").replace("\r\n", "\n").strip()
    if not body:
        raise CommentError("Write something before posting.")
    if len(body) > BODY_MAX:
        raise CommentError(f"Comments can be at most {BODY_MAX} characters; this one has {len(body)}.")
    return body


def _check(org, user) -> None:
    if not has_perm(user, org, "edit"):
        raise CommentError(f"Your role in {org.name} lets you read comments but not write them "
                           f"(you may not {PERMISSION_TEXT['edit']}).")


def _audit_data(c: Comment) -> dict:
    data = {"where": target_label(c)}
    if c.shipment_id:
        data["shipment"] = c.shipment.reference
    if c.document_id:
        data["document"] = c.document.original_filename
    return data


def create(org, user, body: str, *, shipment=None, document=None, parent: Comment | None = None) -> Comment:
    _check(org, user)
    body = _clean(body)
    if parent is not None:
        if parent.organization_id != org.pk or parent.is_deleted:
            raise CommentError("That comment no longer exists, so you can't reply to it.")
        parent = parent.parent or parent  # replies stay one level deep
        shipment, document = parent.shipment, parent.document
    if (shipment is None) == (document is None):
        raise CommentError("Choose what the comment is about.")
    target = shipment or document
    if target.organization_id != org.pk:
        raise CommentError("That shipment or document isn't in this organization.")
    mentioned = parse_mentions(org, body)
    with transaction.atomic():
        c = Comment.objects.create(organization=org, shipment=shipment, document=document, parent=parent,
                                   author=user, body=body)
        c.mentions.set(mentioned)
        audit(org, "comment.created", c, actor=user, mentions=[u.get_username() for u in mentioned],
              mentioned_names=[display(u) for u in mentioned if u.pk != user.pk],
              reply_to=parent.pk if parent else None, **_audit_data(c))
    _notify(c, user, mentioned, replied_to=parent)
    return c


def edit(comment: Comment, user, body: str) -> Comment:
    if not can_change(comment, user):
        raise CommentError(_too_late(comment, user, "edit"))
    _check(comment.organization, user)
    body = _clean(body)
    if body == comment.body:
        return comment
    before = set(comment.mentions.values_list("pk", flat=True))
    mentioned = parse_mentions(comment.organization, body)
    new = [u for u in mentioned if u.pk not in before]
    with transaction.atomic():
        previous = comment.body
        comment.body, comment.edited_at = body, timezone.now()
        comment.save(update_fields=["body", "edited_at"])
        comment.mentions.set(mentioned)
        audit(comment.organization, "comment.edited", comment, actor=user, previous=previous[:2000],
              mentions=[u.get_username() for u in mentioned],
              mentioned_names=[display(u) for u in new if u.pk != user.pk], **_audit_data(comment))
    _notify(comment, user, new, replied_to=None)
    return comment


def delete(comment: Comment, user) -> Comment:
    if not can_change(comment, user):
        raise CommentError(_too_late(comment, user, "delete"))
    with transaction.atomic():
        previous = comment.body
        comment.body, comment.deleted_at = "", timezone.now()
        comment.save(update_fields=["body", "deleted_at"])
        comment.mentions.clear()
        audit(comment.organization, "comment.deleted", comment, actor=user, previous=previous[:2000],
              **_audit_data(comment))
    Notification.objects.filter(object_type="Comment", object_id=str(comment.pk), read_at__isnull=True
                                ).update(read_at=timezone.now())
    return comment


def _too_late(comment: Comment, user, verb: str) -> str:
    if comment.is_deleted:
        return "That comment was already deleted."
    if comment.author_id != getattr(user, "pk", None):
        return f"You can only {verb} your own comments."
    minutes = int(edit_window().total_seconds() // 60)
    return f"Comments can be changed for {minutes} minutes after posting. Add a new comment instead."


def _notify(c: Comment, author, mentioned: list, replied_to: Comment | None) -> None:
    where = target_label(c)
    excerpt = (c.body or "").strip()
    excerpt = excerpt if len(excerpt) <= 240 else excerpt[:239] + "…"
    path = comment_path(c)
    told = set()
    for u in mentioned:
        if u.pk == author.pk:
            continue
        notify_user(u, c.organization, Notification.Kind.MENTION, f"{display(author)} mentioned you on {where}",
                    body=excerpt, url=path, actor=author, obj=c)
        told.add(u.pk)
    if replied_to is not None and replied_to.author_id and replied_to.author_id not in told | {author.pk}:
        notify_user(replied_to.author, c.organization, Notification.Kind.REPLY,
                    f"{display(author)} replied to your comment on {where}", body=excerpt, url=path, actor=author,
                    obj=c)


# --------------------------------------------------------------------------- reading


@dataclass
class Thread:
    comment: Comment
    html: str
    replies: list = field(default_factory=list)


def threads_for(*, shipment=None, document=None) -> list[Thread]:
    if shipment is not None:
        q = Q(shipment=shipment) | Q(document__match__shipment=shipment)
        org_id = shipment.organization_id
    else:
        q = Q(document=document)
        org_id = document.organization_id
    comments = list(Comment.objects.filter(q, organization_id=org_id)
                    .select_related("author", "document", "shipment").prefetch_related("mentions")
                    .order_by("created_at", "pk"))
    tops: dict[int, Thread] = {}
    replies = []
    for c in comments:
        if c.parent_id is None:
            tops[c.pk] = Thread(c, render(c))
        else:
            replies.append(c)
    for r in replies:
        t = tops.get(r.parent_id)
        if t is not None:
            t.replies.append(Thread(r, render(r)))
    # A deleted comment with no replies left is just noise.
    return [t for t in tops.values() if not (t.comment.is_deleted and not t.replies)]
