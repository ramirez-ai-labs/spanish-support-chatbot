"""Ports the core depends on. Adapters in `adapters/` implement them per provider or cloud."""

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Protocol

from core.identity import Caller, VerifiedToken
from core.tools import ToolResult, ToolSpec
from core.usage import LLMUsage, TokenUsage


class LLMProvider(Protocol):
    """Streams a model reply as text chunks.

    The core only sees text; provider SDKs, keys and model settings stay inside the adapter. When
    `usage` is given, the adapter fills it with the provider's token counts by the end of the
    stream; the core measures time and outcome and records the call through a UsageSink.
    """

    model: str  # model name, for traces and usage records

    def stream(
        self, system: str, user_message: str, usage: TokenUsage | None = None
    ) -> AsyncIterator[str]: ...


class UsageSink(Protocol):
    """Stores one LLMUsage per model call (llm_usage in Neon).

    `record` must never raise: losing a usage row is better than failing a chat. Implementations
    log failures by error type only.
    """

    async def record(self, usage: LLMUsage) -> None: ...


class IdentityProvider(Protocol):
    """Verifies a bearer token: signature, issuer, audience, expiry and the claims the tiers need.

    Raises `core.identity.InvalidToken` when any check fails. Tokens never go past this port: the
    core gets a `VerifiedToken`, and the LLM never sees either.
    """

    def verify(self, token: str) -> VerifiedToken: ...


class ToolProvider(Protocol):
    """The tools available to a caller, in-process for customers and over MCP for staff (M9).

    Both methods take the verified caller. Every implementation enforces `core.tools.check_allowed`,
    and the contract suite proves it, so moving a tool behind MCP doesn't change who may call it.
    """

    def list_tools(self, caller: Caller) -> Sequence[ToolSpec]:
        """Only the tools this caller may use; the model is never shown the others."""
        ...

    async def call(
        self, caller: Caller, name: str, arguments: Mapping[str, Any], confirmed: bool = False
    ) -> ToolResult:
        """Runs a tool. Raises `UnknownTool`, or `ToolDenied` with a reason code."""
        ...
