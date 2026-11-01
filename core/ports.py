"""Ports the core depends on. Adapters in `adapters/` implement them per provider or cloud."""

from collections.abc import AsyncIterator
from typing import Protocol

from core.identity import VerifiedToken


class LLMProvider(Protocol):
    """Streams a model reply as text chunks.

    The core only sees text; provider SDKs, keys and model settings stay inside the adapter.
    Usage recording (tokens, purpose, latency) is added here in M1.
    """

    model: str  # model name, for traces and usage records

    def stream(self, system: str, user_message: str) -> AsyncIterator[str]: ...


class IdentityProvider(Protocol):
    """Verifies a bearer token: signature, issuer, audience, expiry and the claims the tiers need.

    Raises `core.identity.InvalidToken` when any check fails. Tokens never go past this port: the
    core gets a `VerifiedToken`, and the LLM never sees either.
    """

    def verify(self, token: str) -> VerifiedToken: ...
