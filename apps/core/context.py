"""Per-request context (request ID, client IP) available anywhere, e.g. to the audit log and logging."""
from __future__ import annotations

import contextvars
import logging

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
client_ip_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("client_ip", default=None)


class RequestIdFilter(logging.Filter):
    """Adds request_id to every log record so log lines from one request can be grouped."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        return True
