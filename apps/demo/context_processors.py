from django.conf import settings

from .services.accounts import demo_accounts


def demo(request):
    """Demo banner, demo accounts on the sign-in page and the try page link."""
    return {
        "demo_mode": settings.DEMO_MODE,
        "demo_accounts": demo_accounts() if settings.DEMO_MODE else [],
        "try_enabled": settings.TRY_ENABLED,
        "contact_url": settings.DEMO_CONTACT_URL,
    }
