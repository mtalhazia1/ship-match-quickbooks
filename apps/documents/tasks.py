import logging

from celery import shared_task
from django.db import OperationalError

from apps.core.models import Organization

log = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(ConnectionError, TimeoutError, OperationalError), retry_backoff=True,
             retry_jitter=True, max_retries=5)
def process_document_task(self, doc_id: int):
    from apps.documents.services.pipeline import process_document

    doc = process_document(doc_id)
    return {"document": doc_id, "status": doc.status}


@shared_task
def poll_mailbox_task(org_id: int):
    """Kept for tasks queued before email intake moved to apps.mailboxes: checks the org's Gmail mailbox."""
    from apps.mailboxes.services import gmail
    from apps.mailboxes.services.polling import poll_mailbox

    mailbox = gmail.sync(Organization.objects.get(pk=org_id))
    return poll_mailbox(mailbox) if mailbox else {"status": "skipped"}


@shared_task
def poll_all_mailboxes():
    """Kept for older beat schedules: every mailbox is now checked by apps.mailboxes.tasks.poll_all_mailboxes."""
    from apps.mailboxes.tasks import poll_all_mailboxes as poll_all

    return poll_all()
