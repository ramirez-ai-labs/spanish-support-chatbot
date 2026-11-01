"""The bot check in front of the chat: anonymous visitors prove they're human (Turnstile) first."""


class BotCheckFailed(Exception):
    """The request didn't pass the bot check. `reason` is a short code, safe to log and return."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
