"""Chat orchestration: retrieval for the visitor's market, then one model call, streamed (ADR 0007).

With a market, the question's nearest policy chunks go to the model after the stable system
prompt, and the reply starts with a `Sources` event naming them. Without one, the model answers
alone, as before M2: the chat page sends a market from #89, which makes it required.

Personal data is redacted first (ADR 0008): `prepare` runs before the stream starts, and `reply`
accepts only its `Redacted` result, so retrieval, the query vector and the model never see the raw
text. The answer is scanned as it streams, holding back its last 64 characters, so an ID the model
writes never reaches the page whole. Tools come later in M3.

With sessions (ADR 0008), `prepare` also opens the conversation: it loads while the NER runs, then
numbers the placeholders, so a value keeps its `<TYPE_n>` on every turn. The model gets the last
turns as its own user and model messages, and a completed answer is saved as the next turn.
Memory is a convenience and redaction isn't: if the store can't be reached the answer goes on
without history and nothing is saved, while a failed redaction still refuses the message.

Each step is one span (OpenTelemetry API only, which is cloud-free), so time to first token breaks
down by stage. Spans carry sizes, scores, counts and timings, never message or chunk text. Every
model call also writes one usage record, after the answer has streamed.
"""

import asyncio
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from core.identity import Caller
from core.ports import LLMProvider, UsageSink
from core.redaction import Found, Redacted, Redactor, summary, unredacted
from core.retrieval import RetrievedChunk, Retriever
from core.safety import ContentBlocked
from core.sessions import Session, SessionConflict, SessionRefused, Sessions
from core.usage import LLMUsage, Outcome, TokenUsage

tracer = trace.get_tracer(__name__)
logger = logging.getLogger(__name__)

# Kept identical across calls so the prefix can be cached once prompt caching is wired in.
SYSTEM_PROMPT = (
    "Eres el asistente de soporte de un banco ficticio para clientes hispanohablantes. "
    "Responde siempre en español, de forma breve, clara y amable. "
    "Si no sabes algo o la pregunta requiere acceso a la cuenta del cliente, dilo y ofrece "
    "comunicar al cliente con un agente humano. "
    "Cuando el mensaje traiga fuentes, responde solo con lo que dicen. Si no cubren la pregunta, "
    "o son «ninguna», di que no tienes esa información y ofrece comunicar al cliente con un "
    "agente humano; no la completes con lo que sepas por tu cuenta. No cites las fuentes por "
    "número: el cliente las ve debajo de tu respuesta. "
    "Si el cliente solo saluda, salúdalo y pregúntale en qué puedes ayudar. "
    "Nunca pidas números completos de tarjeta, contraseñas ni documentos de identidad. "
    "Los marcadores como <PERSON_1> o <MX_CURP_1> son datos protegidos del cliente: no los "
    "repitas ni intentes adivinar qué ocultan."
)

# What the prompt is made of, recorded by name (never content) with each call.
PROMPT_PARTS = ("system", "user")
PROMPT_PARTS_WITH_CONTEXT = ("system", "context", "user")

# Session loads run here, beside the NER. A load is one short read, so a few threads keep up with
# the 20 requests an instance serves at once.
_SESSION_LOADS = ThreadPoolExecutor(max_workers=8, thread_name_prefix="session-load")

MAX_MESSAGE_CHARS = 2000


class KnowledgeUnavailable(Exception):
    """Retrieval failed (e.g. the knowledge store is unreachable). The chat stops rather than
    answering without the market's policy. Carries no detail: the cause can quote the question."""


@dataclass(frozen=True)
class Source:
    """One policy section the answer is based on, as the page shows it. No IDs or scores."""

    title: str
    section: str
    market: str


@dataclass(frozen=True)
class Sources:
    """The first event of a reply with a market: what the model was given, even if nothing."""

    sources: tuple[Source, ...]


@dataclass(frozen=True)
class Prepared:
    """A message ready for `reply`: redacted, with its conversation when sessions are on."""

    redacted: Redacted
    session: Session | None = None  # None: no sessions, or the store couldn't be reached


def prompt_parts(history: bool, context: bool) -> tuple[str, ...]:
    """The prompt's parts by name, in order: what usage records say it was made of."""
    return (
        "system",
        *(("history",) if history else ()),
        *(("context",) if context else ()),
        "user",
    )


def sources_for(chunks: Sequence[RetrievedChunk]) -> Sources:
    """One entry per section, in retrieval order: a long section's pieces show once."""
    seen: dict[Source, None] = {}
    for chunk in chunks:
        seen.setdefault(Source(chunk.title, " > ".join(chunk.heading_path), chunk.market))
    return Sources(tuple(seen))


def user_message(question: str, chunks: Sequence[RetrievedChunk]) -> str:
    """The question after its sources, so the system prompt stays the same on every call."""
    if not chunks:
        return f"Fuentes: ninguna.\n\nPregunta del cliente: {question}"
    blocks = [
        f"[{i}] {chunk.title} — {' > '.join(chunk.heading_path)}\n{chunk.content}"
        for i, chunk in enumerate(chunks, start=1)
    ]
    return "Fuentes:\n\n" + "\n\n".join(blocks) + f"\n\nPregunta del cliente: {question}"


class ChatService:
    def __init__(
        self,
        llm: LLMProvider,
        usage: UsageSink | None = None,
        cloud: str = "local",
        retriever: Retriever | None = None,
        redactor: Redactor | None = None,
        sessions: Sessions | None = None,
    ):
        self._llm = llm
        self._usage = usage
        self._cloud = cloud
        self._retriever = retriever
        self._redactor = redactor
        self._sessions = sessions

    def redact(self, text: str) -> Redacted:
        """The message with its personal data replaced, before anything else reads it.

        CPU-bound (spaCy): call it off the event loop. Raises `core.pii.DetectorUnavailable` if
        the detector fails, and the caller refuses the message: unredacted text goes no further.
        Without a redactor (tests, local runs) the text passes as it is; the API refuses to start
        that way on a cloud.
        """
        found = self._find(text)
        return found.redact() if found is not None else unredacted(text)

    def prepare(self, text: str, caller: Caller, session_id: str | None = None) -> Prepared:
        """The message redacted, with the caller's conversation when sessions are on.

        The session loads in another thread while the NER runs, then numbers the placeholders. A
        session ID that's unknown, expired or malformed starts a new conversation, and so does one
        that belongs to someone else: signing in or out mid-conversation starts afresh, as ADR 0008
        says, and nobody learns whose it was. Raises as `redact` does; a store that can't be
        reached only costs the history. CPU-bound and blocking: call it off the event loop.
        """
        if self._sessions is None:
            return Prepared(self.redact(text))
        loading = _SESSION_LOADS.submit(
            self._open_session, caller, session_id, otel_context.get_current()
        )
        found = self._find(text)  # if it raises, the load finishes on its own and is dropped
        session = loading.result()
        if found is None:
            return Prepared(unredacted(text), session)
        numbering = self._sessions.numbering(session) if session is not None else None
        return Prepared(found.redact(numbering), session)

    def _find(self, text: str) -> Found | None:
        if self._redactor is None:
            return None
        # record_exception=False: a detector error can quote the text.
        with tracer.start_as_current_span(
            "chat.redaction", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("redaction.stage", "input")
            try:
                found = self._redactor.find(text)
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", type(exc).__name__)
                raise
            counts = Counter(s.entity_type for s in found.spans)
            span.set_attribute("redaction.count", len(found.spans))
            span.set_attribute("redaction.types", sorted(counts))
            return found

    def _open_session(
        self, caller: Caller, session_id: str | None, parent: otel_context.Context
    ) -> Session | None:
        assert self._sessions is not None
        attached = otel_context.attach(parent)  # this thread's span joins the request's trace
        try:
            with tracer.start_as_current_span(
                "chat.session_load", record_exception=False, set_status_on_exception=False
            ) as span:
                refused = False
                try:
                    session = self._sessions.open(caller, session_id)
                except SessionRefused:
                    session, refused = self._sessions.start(caller), True
                    logger.info("session_refused")  # someone else's: a new one, and no detail
                except Exception as exc:  # SessionUnavailable, or anything else from the store
                    cause = type(exc.__cause__ or exc).__name__
                    logger.warning("session_unavailable type=%s", cause)
                    span.set_status(Status(StatusCode.ERROR))
                    span.set_attribute("error.type", cause)
                    return None
                # Never the ID: whoever holds it can write to an anonymous conversation.
                span.set_attribute("session.new", session.version == 0)
                span.set_attribute("session.refused", refused)
                span.set_attribute("session.turns", len(session.turns))
                return session
        finally:
            otel_context.detach(attached)

    def _save(self, session: Session) -> None:
        """Stores the finished turn. It never fails a chat: the answer has already been shown."""
        assert self._sessions is not None
        with tracer.start_as_current_span(
            "chat.session_save", record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                self._sessions.save(session)
            except SessionConflict:
                # Another request on this conversation saved first, or it expired and was swept:
                # this turn isn't kept, and the next request carries on from what was stored.
                logger.info("session_conflict")
                span.set_attribute("session.conflict", True)
            except Exception as exc:
                cause = type(exc.__cause__ or exc).__name__
                logger.warning("session_save_error type=%s", cause)
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", cause)

    async def reply(
        self, redacted: Redacted, market: str | None = None, session: Session | None = None
    ) -> AsyncIterator[str | Sources]:
        """Streams the answer's text, after a `Sources` event when the market is retrieved from.

        Without a market or a retriever, the model answers alone. The market must already be a
        known one; the API refuses others before streaming. With a session, the model gets its
        last turns, and an answer that completes is saved as the next one.
        """
        message = redacted.text
        history = tuple(session.turns) if session is not None else ()
        retrieved: list[RetrievedChunk] | None = None
        prompt = message
        if market is not None and self._retriever is not None:
            retrieved = await self._retrieve(message, market)
            prompt = user_message(message, retrieved)
            yield sources_for(retrieved)
        parts = prompt_parts(history=bool(history), context=retrieved is not None)

        tokens = TokenUsage()
        outcome: Outcome = "incomplete"  # stays so if the client leaves mid-stream
        # Without a redactor (tests, local runs) there's nothing to scan for and no placeholders.
        scanner = self._redactor.output_scanner() if self._redactor is not None else None
        ttft_ms: int | None = None
        blocked: tuple[str, ...] = ()
        answer: list[str] = []  # what the visitor saw, which is what the session keeps
        # record_exception=False: the exception message can quote the user's text.
        with tracer.start_as_current_span(
            "chat.model", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("gen_ai.request.model", self._llm.model)
            span.set_attribute("chat.message_chars", len(message))
            start = time.perf_counter()
            chunks = 0
            shown = False
            try:
                async for delta in self._llm.stream(
                    SYSTEM_PROMPT, prompt, usage=tokens, history=history
                ):
                    if chunks == 0:
                        ttft_ms = round((time.perf_counter() - start) * 1000)
                        span.set_attribute("llm.time_to_first_token_ms", ttft_ms)
                    chunks += 1
                    # A pattern scan of ~100 characters takes 1-2 ms: on the loop, not a thread.
                    released = scanner.feed(delta) if scanner is not None else delta
                    if released:
                        if not shown:  # what the visitor waits for: after the hold-back
                            shown = True
                            span.set_attribute(
                                "chat.time_to_first_text_ms",
                                round((time.perf_counter() - start) * 1000),
                            )
                        answer.append(released)
                        yield released
                if scanner is not None and (rest := scanner.finish()):
                    if not shown:  # a short answer is released whole, at the end
                        span.set_attribute(
                            "chat.time_to_first_text_ms",
                            round((time.perf_counter() - start) * 1000),
                        )
                    answer.append(rest)
                    yield rest
                outcome = "done"
            except ContentBlocked as exc:
                outcome, blocked = "blocked", exc.categories
                span.set_attribute("chat.blocked_stage", exc.stage)
                span.set_attribute("chat.blocked_categories", list(exc.categories))
                raise
            except Exception as exc:
                outcome = "error"
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", type(exc).__name__)
                raise
            finally:
                if scanner is not None:
                    span.set_attribute("redaction.output_count", sum(scanner.counts.values()))
                    if scanner.counts:  # types and counts, never the values
                        logger.info("pii_redacted stage=output types=%s", summary(scanner.counts))
                span.set_attribute("llm.output_chunks", chunks)
                span.set_attribute("gen_ai.usage.input_tokens", tokens.input_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", tokens.output_tokens)
                if self._usage is not None:
                    context = span.get_span_context()
                    record = LLMUsage(
                        cloud=self._cloud,
                        model=self._llm.model,
                        purpose="chat",
                        prompt_parts=parts,
                        tokens=tokens,
                        latency_ms=round((time.perf_counter() - start) * 1000),
                        ttft_ms=ttft_ms,
                        outcome=outcome,
                        trace_id=f"{context.trace_id:032x}" if context.is_valid else None,
                        blocked_categories=blocked,
                    )
                    # Shielded: if the client disconnects and the request is cancelled, the row
                    # is still written.
                    await asyncio.shield(self._usage.record(record))

        # Only a completed answer becomes a turn: a blocked, failed or abandoned one is never kept.
        if session is not None and self._sessions is not None:
            session.add_turn(redacted, "".join(answer))
            await asyncio.to_thread(self._save, session)

    async def _retrieve(self, message: str, market: str) -> list[RetrievedChunk]:
        assert self._retriever is not None
        # record_exception=False: a store error can quote the query.
        with tracer.start_as_current_span(
            "chat.retrieval", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("chat.market", market)
            span.set_attribute("retrieval.k", self._retriever.k)
            span.set_attribute("retrieval.min_score", self._retriever.min_score)
            try:
                # Embedding is CPU work and the store call blocks: off the event loop, so other
                # streams on this instance keep flowing.
                chunks = await asyncio.to_thread(self._retriever.retrieve, message, market)
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", type(exc).__name__)
                raise KnowledgeUnavailable() from exc
            span.set_attribute("retrieval.chunks", len(chunks))
            if chunks:
                span.set_attribute("retrieval.top_score", round(chunks[0].score, 4))
            return chunks
