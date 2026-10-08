class MailboxError(RuntimeError):
    """A problem checking a mailbox, worded for the person who manages it."""


class MailboxAuthError(MailboxError):
    """The mail system no longer accepts the saved sign-in; someone has to connect the mailbox again."""


class MailboxThrottled(MailboxError):
    """The mail system asked us to slow down; the next scheduled check continues where this one stopped."""
