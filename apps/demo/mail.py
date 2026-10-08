"""Email backend for the public demo: anyone can sign in with the shared accounts, so nothing they write
(dispute emails, alert tests, digests) may leave the server. Messages are logged, never sent."""
import logging

from django.core.mail.backends.base import BaseEmailBackend

log = logging.getLogger("shipmatch.demo")


class DemoEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        for m in email_messages or []:
            log.info("Demo mode: email not sent (subject %r, %d recipient(s))", (m.subject or "")[:80],
                     len(m.recipients()))
        return len(email_messages or [])


def outbound_blocked() -> bool:
    """True on a public demo unless the operator explicitly allows real email and webhooks."""
    from django.conf import settings

    return bool(getattr(settings, "DEMO_MODE", False)) and not getattr(settings, "DEMO_SEND_OUTSIDE", False)
