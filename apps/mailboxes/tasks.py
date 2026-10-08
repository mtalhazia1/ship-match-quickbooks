import logging

from celery import shared_task

from apps.core.models import Organization

log = logging.getLogger(__name__)


@shared_task(acks_late=True)
def poll_mailbox_task(mailbox_id: int):
    from apps.mailboxes.services.polling import poll_mailbox

    return poll_mailbox(mailbox_id)


@shared_task
def poll_all_mailboxes():
    """Queue a check of every enabled mailbox. A mailbox that fails (or fails to queue) never stops the others."""
    from apps.mailboxes.services import gmail
    from apps.mailboxes.services.polling import due_mailboxes

    for org in Organization.objects.all():
        try:
            gmail.sync(org)
        except Exception:
            log.exception("Couldn't sync the Gmail mailbox of %s", org.slug)
    queued, failed = 0, 0
    for mailbox in due_mailboxes():
        try:
            poll_mailbox_task.delay(mailbox.pk)
            queued += 1
        except Exception:   # eager mode runs the task here; a broker outage raises here
            failed += 1
            log.exception("Couldn't check mailbox %s", mailbox.pk)
    return {"queued": queued, "failed": failed}
