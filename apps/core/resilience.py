"""Keep requests working while Redis (the Celery broker and the cache) is unreachable.

A broker outage must not turn an upload or a button into a 500 (QA-091, QA-095), and a cache outage must not
stop people signing in or integrations calling the API (QA-094). Stored work is picked up again by the sweeps
(apps.documents.tasks.requeue_stalled_documents, the alert and webhook retry tasks).
"""
from __future__ import annotations

import logging

from kombu.exceptions import OperationalError as _KombuError

try:
    from redis.exceptions import RedisError as _RedisError
except ImportError:  # redis is installed with celery[redis]; keep the module importable without it
    class _RedisError(Exception):
        pass

log = logging.getLogger(__name__)

BROKER_ERRORS = (_KombuError, ConnectionError, TimeoutError)
CACHE_ERRORS = (_RedisError, ConnectionError, TimeoutError)


def enqueue(task, *args, **kwargs) -> bool:
    """`task.delay(*args, **kwargs)`, or False (logged) when the broker can't be reached."""
    try:
        task.delay(*args, **kwargs)
    except BROKER_ERRORS:
        log.warning("Could not queue %s%s: broker unreachable", task.name, args, exc_info=True)
        return False
    return True


NOT_QUEUED = "Couldn't start it because the background service is unavailable. Try again in a minute."
