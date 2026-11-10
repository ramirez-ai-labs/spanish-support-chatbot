"""Ports the core depends on. Adapters in `adapters/` implement them per provider or cloud."""

from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from core.identity import Caller, VerifiedToken
from core.pii import PIISpan
from core.tools import ToolResult, ToolSpec
from core.usage import LLMUsage, TokenUsage

if TYPE_CHECKING:  # core.indexing, core.retrieval and core.sessions import this module
    from core.indexing import StoredChunk
    from core.retrieval import RetrievedChunk
    from core.sessions import Session, Turn


class LLMProvider(Protocol):
    """Streams a model reply as text chunks.

    The core only sees text; provider SDKs, keys and model settings stay inside the adapter. When
    `usage` is given, the adapter fills it with the provider's token counts by the end of the
    stream; the core measures time and outcome and records the call through a UsageSink. When the
    provider's safety filters block the message or the answer, the stream raises
    `core.safety.ContentBlocked`, even if some text was already streamed.

    `history` is the conversation so far, oldest first. The adapter sends each turn as the
    provider's own user and model messages, never pasted into `user_message`: text the customer
    typed can then never pose as an earlier answer or as the sources.
    """

    model: str  # model name, for traces and usage records

    def stream(
        self,
        system: str,
        user_message: str,
        usage: TokenUsage | None = None,
        history: Sequence["Turn"] = (),
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

    def market_counts(self) -> dict[str, int]:
        """How many chunks each market has, `all` included; markets with none are left out."""
        ...

    def apply(self, insert: Sequence["StoredChunk"], delete: Collection[str]) -> None: ...


class KnowledgeStore(Protocol):
    """Finds the chunks nearest to a query vector, in the given markets only (ADR 0007).

    `markets` is the visitor's market plus `all`; every adapter must return chunks from those
    markets and no others, and refuse an empty list. That's the correctness boundary for M2, and
    the contract suite checks it. Only vectors from `model` are compared. Results are sorted by
    cosine similarity, highest first, at most `k` of them.
    """

    def search(
        self, vector: Sequence[float], markets: Sequence[str], k: int, model: str
    ) -> list["RetrievedChunk"]: ...


class UsageSink(Protocol):
    """Stores one LLMUsage per model call (llm_usage in Neon).

    `record` must never raise: losing a usage row is better than failing a chat. Implementations
    log failures by error type only.
    """

    async def record(self, usage: LLMUsage) -> None: ...


class SessionStore(Protocol):
    """Keeps conversation sessions, keyed by their random ID (ADR 0008).

    Stores and returns `core.sessions.to_record`'s data unchanged; the rules on owners and contents
    are in `core.sessions`. Every method blocks: the API calls them off the event loop. Raises
    `core.sessions.SessionUnavailable` if the store can't be reached, and never logs a session's
    contents.
    """

    def load(self, session_id: str, now: float) -> "Session | None":
        """The session, or None if it's unknown or has expired by `now`, even if not yet deleted."""
        ...

    def save(self, session: "Session") -> None:
        """Stores it if it's unchanged since it was loaded, and raises its `version` by one.

        Version 0 creates it. Raises `core.sessions.SessionConflict`, storing nothing, if the stored
        version differs: another request saved first, or the session was deleted.
        """
        ...

    def delete_expired(self, now: float, limit: int) -> int:
        """Deletes up to `limit` sessions expired by `now`, and returns how many."""
        ...


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


class PIIDetector(Protocol):
    """Finds personal data in Spanish text: where it is, what type, and how sure (ADR 0008).

    Runs locally with no network call, ever. It reports every span it finds; the core picks the
    types and threshold. Raises `core.pii.DetectorUnavailable` if it can't run, so the caller
    fails closed. CPU-bound: the API calls it off the event loop. Never logs the text.
    """

    def detect(self, text: str) -> list[PIISpan]: ...

    def detect_patterns(self, text: str) -> list[PIISpan]:
        """Only the pattern recognizers (IDs, cards, email, phone), with no NER: for the model's
        output, scanned as it streams. Cheap enough to run on every chunk."""
        ...


class DeployProbe(Protocol):
    """Recognizes the deploy pipeline's own signed identity, which may skip the bot check.

    Raises `core.identity.InvalidToken` for anything else. A probe still gets the anonymous tier:
    it only proves the request comes from the approved deploy job, not that it's a customer.
    """

    def verify(self, token: str) -> None: ...
