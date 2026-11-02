"""Email-link sign-in: the API sends the link after the bot check, so the page's key can't (#72).

An email address is personal data: it never goes in logs, spans or error messages.
"""

import re

MAX_EMAIL_CHARS = 254  # the longest address SMTP can deliver to

# A shape check only, to refuse obvious typos before calling the provider; the provider decides.
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def looks_like_email(value: str) -> bool:
    return len(value) <= MAX_EMAIL_CHARS and _EMAIL.fullmatch(value) is not None


class SignInLinkFailed(Exception):
    """The link wasn't sent. `reason` is a short code, safe to log and return: `invalid_email`,
    `too_many_attempts` or `sign_in_unavailable`. It never carries the address."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
