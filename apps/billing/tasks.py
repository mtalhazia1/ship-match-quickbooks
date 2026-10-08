"""Background work for sign-up and billing (Celery beat)."""
from celery import shared_task


@shared_task(ignore_result=True)
def purge_pending_signups() -> int:
    """Delete unverified sign-ups (and their password hashes) once their link has expired."""
    from .signup import purge

    return purge()
