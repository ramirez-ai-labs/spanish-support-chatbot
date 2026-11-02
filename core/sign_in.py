"""Email-link sign-in: the API sends the link after the bot check, so the page's key can't (#72).

An email address is personal data: it never goes in logs, spans or error messages.
"""

MAX_EMAIL_CHARS = 254  # the longest address SMTP can deliver to


def looks_like_email(value: str) -> bool:
    """A shape check only, to refuse obvious typos before calling the provider; the provider
    decides. String operations, not a regex, so the time stays linear in the input."""
    local, at, domain = value.partition("@")
    return (
        len(value) <= MAX_EMAIL_CHARS
        and bool(local and at)
        and "@" not in domain
        and "." in domain.strip(".")  # a dot inside the domain, not only at its ends
        and not any(c.isspace() for c in value)
    )


class SignInLinkFailed(Exception):
    """The link wasn't sent. `reason` is a short code, safe to log and return: `invalid_email`,
    `too_many_attempts` or `sign_in_unavailable`. It never carries the address."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
