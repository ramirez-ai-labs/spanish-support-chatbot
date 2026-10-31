"""Ports the core depends on. Adapters in `adapters/` implement them per provider or cloud."""

from collections.abc import AsyncIterator
from typing import Protocol


class LLMProvider(Protocol):
    """Streams a model reply as text chunks.

    The core only sees text; provider SDKs, keys and model settings stay inside the adapter.
    Usage recording (tokens, purpose, latency) is added here in M1.
    """

    def stream(self, system: str, user_message: str) -> AsyncIterator[str]: ...
