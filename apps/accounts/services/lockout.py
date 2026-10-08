"""Brute-force protection: lock a username (and an IP) after repeated failed sign-ins.

Counters live in the Django cache. Use Redis (CACHE_URL) in production so all web
workers share them; the local-memory default is per process.
"""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache


def _keys(username: str, ip: str | None) -> tuple[str, str]:
    return f"login-fail:user:{(username or '').strip().lower()}", f"login-fail:ip:{ip or '-'}"


def is_locked(username: str, ip: str | None) -> bool:
    user_key, ip_key = _keys(username, ip)
    return (cache.get(user_key, 0) >= settings.LOGIN_MAX_FAILURES_PER_USER
            or cache.get(ip_key, 0) >= settings.LOGIN_MAX_FAILURES_PER_IP)


def register_failure(username: str, ip: str | None) -> int:
    """Count one failure. Returns the failure count for this username."""
    user_key, ip_key = _keys(username, ip)
    window = settings.LOGIN_LOCKOUT_SECONDS
    for key in (user_key, ip_key):
        if cache.add(key, 1, window) is False:
            try:
                cache.incr(key)
            except ValueError:
                cache.set(key, 1, window)
    return cache.get(user_key, 0)


def reset(username: str, ip: str | None) -> None:
    user_key, _ = _keys(username, ip)
    cache.delete(user_key)
