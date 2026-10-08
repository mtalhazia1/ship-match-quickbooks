from celery import shared_task


@shared_task(ignore_result=True)
def email_notification(notification_id: int) -> bool:
    """Email one personal notification (a mention, a reply, an assignment) to its person."""
    from .services.notify import send_email

    return send_email(notification_id)
