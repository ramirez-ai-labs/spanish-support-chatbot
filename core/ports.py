"""Ports the core depends on. Adapters in `adapters/` implement them per provider or cloud."""

from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from core.identity import Caller, VerifiedToken
from core.tools import ToolResult, ToolSpec
from core.usage import LLMUsage, TokenUsage

if TYPE_CHECKING:  # core.indexing imports this module
    from core.indexing import StoredChunk


class LLMProvider(Protocol):
    """Streams a model reply as text chunks.

    The core only sees text; provider SDKs, keys and model settings stay inside the adapter. When
    `usage` is given, the adapter fills it with the provider's token counts by the end of the
    stream; the core measures time and outcome and records the call through a UsageSink. When the
    provider's safety filters block the message or the answer, the stream raises
    `core.safety.ContentBlocked`, even if some text was already streamed.
    """

    model: str  # model name, for traces and usage records

    def stream(
        self, system: str, user_message: str, usage: TokenUsage | None = None
    ) -> AsyncIterator[str]: ...


class Embedder(Protocol):
    """Turns text into unit-length vectors for retrieval (ADR 0007).

    Passages (when indexing) and queries (at question time) are embedded differently, so they are
    separate methods; the adapter adds whatever its model needs, e.g. e5's `passage:` and `query:`
    prefixes. `model` names the model and its version: each chunk records it, and vectors are
    only ever compared with the same model's. CPU-bound: the API calls it off the event loop.
    """

    model: str
    dimensions: int

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class ChunkStore(Protocol):
    """Where the indexer keeps knowledge chunks and their vectors (the `chunks` table in Neon).

    Chunks are identified by content hash. `apply` inserts and deletes in one transaction, so a
    failed run leaves the store as it was, and the API never sees a half-indexed doc.
    """

    def content_hashes(self) -> set[str]: ...

    def apply(self, insert: Sequence["StoredChunk"], delete: Collection[str]) -> None: ...


class UsageSink(Protocol):
    """Stores one LLMUsage per model call (llm_usage in Neon).

    `record` must never raise: losing a usage row is better than failing a chat. Implementations
    log failures by error type only.
    """

    async def record(self, usage: LLMUsage) -> None: ...


class IdentityProvider(Protocol):
    """Verifies a bearer token: signature, issuer, audience, expiry and the claims the tiers need.

    Raises `core.identity.InvalidToken` when any check fails, and
    `core.identity.IdentityUnavailable` when the provider's keys can't be fetched. Tokens never go
    past this port: the core gets a `VerifiedToken`, and the LLM never sees either.
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


class BotCheck(Protocol):
    """Verifies the human-check token the chat page sends with each message.

    Raises `core.bot_check.BotCheckFailed` with a reason code: the token is missing, invalid, used
    already, from another site, or the check itself is unreachable (which fails closed).
    """

    def verify(self, token: str | None) -> None: ...


class SignInLinkSender(Protocol):
    """Emails a one-time sign-in link. The API calls it only after the bot check passes.

    The link always returns to the chat page: the adapter's config sets where, never the caller.
    Raises `core.sign_in.SignInLinkFailed` with a reason code, and fails closed when the provider
    is unreachable. Implementations never log the address.
    """

    def send(self, email: str) -> None: ...


class DeployProbe(Protocol):
    """Recognizes the deploy pipeline's own signed identity, which may skip the bot check.

    Raises `core.identity.InvalidToken` for anything else. A probe still gets the anonymous tier:
    it only proves the request comes from the approved deploy job, not that it's a customer.
    """

    def verify(self, token: str) -> None: ...
