"""Django system checks for sign-in protection, reported by `manage.py check` (and at start-up)."""
from __future__ import annotations

from django.core.checks import Warning, register


@register()
def shared_cache_in_production(app_configs=None, **kwargs):
    """QA-050: sign-in lockout counters live in the cache. A per-process cache multiplies the limit by the number
    of web workers and forgets it on every restart."""
    from django.conf import settings

    return cache_warnings(settings.DEBUG, settings.CACHES.get("default", {}).get("BACKEND", ""))


def cache_warnings(debug: bool, backend: str) -> list:
    if debug or not backend.endswith(("LocMemCache", "DummyCache")):
        return []
    return [Warning("The cache is local to each process, so sign-in lockout, two-factor replay protection and API "
                    "rate limits are not shared between web workers and reset on restart.",
                    hint="Set CACHE_URL to a Redis server (see .env.example).", id="accounts.W001")]
