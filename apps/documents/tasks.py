import logging

from celery import shared_task
from django.conf import settings
from django.db import OperationalError

from apps.core.models import Organization

log = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(ConnectionError, TimeoutError, OperationalError), retry_backoff=True,
             retry_jitter=True, max_retries=5, soft_time_limit=settings.DOCUMENT_TASK_TIME_LIMIT,
             time_limit=settings.DOCUMENT_TASK_TIME_LIMIT + 60)
def process_document_task(self, doc_id: int):
    from apps.documents.models import Document
    from apps.documents.services.pipeline import process_document

    if not Document.objects.filter(pk=doc_id).exists():   # removed while the task waited (QA-096)
        return {"document": doc_id, "skipped": "no_document"}
    doc = process_document(doc_id)
    return {"document": doc_id, "status": doc.status}


MAX_REQUEUES = 3


@shared_task(ignore_result=True)
def requeue_stalled_documents() -> int:
    """Safety net: documents whose task was never run (broker down at upload, broker restarted with an empty
    queue, worker killed mid-read). Each is queued again up to MAX_REQUEUES times, then shown as an error so
    a file that keeps killing the worker can't do so forever (QA-091)."""
    from django.utils import timezone

    from apps.core.utils import audit
    from apps.documents.models import Document
    from apps.documents.services import ingest

    now = timezone.now()
    stalled = list(Document.objects.filter(status__in=ingest.UNFINISHED, updated_at__lt=now - ingest.STALE_AFTER)
                   .select_related("organization").order_by("updated_at")[:200])
    queued = 0
    for doc in stalled:
        tries = int((doc.intake or {}).get("requeued", 0))
        if tries >= MAX_REQUEUES:
            doc.status = Document.Status.ERROR
            doc.error = (f"Reading this document didn't finish after {tries + 1} attempts. Please contact support "
                         "if it happens again.")
            doc.save(update_fields=["status", "error", "updated_at"])
            audit(doc.organization, "document.error", doc, error="processing stalled", attempts=tries + 1)
            continue
        # Count the attempt and move updated_at, so the next sweep waits another STALE_AFTER for this one.
        Document.objects.filter(pk=doc.pk).update(intake={**(doc.intake or {}), "requeued": tries + 1},
                                                   updated_at=now)
        if not ingest.queue(doc.pk):   # the broker is still down: not an attempt; the next sweep tries again
            Document.objects.filter(pk=doc.pk).update(intake=doc.intake)
            break
        queued += 1
    if stalled:
        log.info("Stalled documents: %s found, %s queued again", len(stalled), queued)
    return queued


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
