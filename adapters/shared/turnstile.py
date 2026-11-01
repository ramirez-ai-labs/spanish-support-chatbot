"""BotCheck with Cloudflare Turnstile: each chat message carries a fresh, single-use token.

Fails closed: if Cloudflare can't be reached, the message is refused rather than let through.
"""

from collections.abc import Collection

import httpx

from core.bot_check import BotCheckFailed

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
MAX_TOKEN_CHARS = 2048  # Cloudflare's documented maximum


class TurnstileBotCheck:
    def __init__(
        self,
        secret: str,
        hostnames: Collection[str],
        client: httpx.Client | None = None,
        timeout_seconds: float = 5,
    ) -> None:
        if not secret:
            raise ValueError("TURNSTILE_SECRET is empty")
        self._secret = secret
        self._hostnames = set(hostnames)
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def verify(self, token: str | None) -> None:
        if not token:
            raise BotCheckFailed("turnstile_required")
        if len(token) > MAX_TOKEN_CHARS:
            raise BotCheckFailed("turnstile_failed")
        try:
            response = self._client.post(
                SITEVERIFY_URL, data={"secret": self._secret, "response": token}
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BotCheckFailed("turnstile_unavailable") from exc
        if result.get("success") is not True:
            raise BotCheckFailed("turnstile_failed")  # invalid, expired or already used
        # A token solved on another site that uses the same widget key must not count here.
        if self._hostnames and result.get("hostname") not in self._hostnames:
            raise BotCheckFailed("turnstile_wrong_host")
