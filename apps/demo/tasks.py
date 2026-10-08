from celery import shared_task
from django.core.management import call_command


@shared_task
def purge_try_uploads():
    """Hourly: delete try page uploads older than TRY_RETENTION_HOURS."""
    from .services.tryit import purge

    return {"deleted": purge()}


@shared_task
def reset_demo():
    """Nightly when DEMO_MODE is on: wipe and rebuild the demo organizations."""
    call_command("reset_demo")
    return {"reset": True}
