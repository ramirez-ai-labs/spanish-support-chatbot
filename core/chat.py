"""Chat orchestration: retrieval for the visitor's market, then one model call, streamed (ADR 0007).

With a market, the question's nearest policy chunks go to the model after the stable system
prompt, and the reply starts with a `Sources` event naming them. Without one, the model answers
alone, as before M2: the chat page sends a market from #89, which makes it required.

Personal data is redacted first (ADR 0008): `redact` runs before the stream starts, and `reply`
accepts only its `Redacted` result, so retrieval, the query vector and the model never see the raw
text. Tools come later in M3.

Each step is one span (OpenTelemetry API only, which is cloud-free), so time to first token breaks
down by stage. Spans carry sizes, scores, counts and timings, never message or chunk text. Every
model call also writes one usage record, after the answer has streamed.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from core.ports import LLMProvider, UsageSink
from core.redaction import Redacted, Redactor, unredacted
from core.retrieval import RetrievedChunk, Retriever
from core.safety import ContentBlocked
from core.usage import LLMUsage, Outcome, TokenUsage

tracer = trace.get_tracer(__name__)

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

# What the prompt is made of, recorded by name (never content) with each call. Sessions add
# "history" in M3.
PROMPT_PARTS = ("system", "user")
PROMPT_PARTS_WITH_CONTEXT = ("system", "context", "user")

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
    ):
        self._llm = llm
        self._usage = usage
        self._cloud = cloud
        self._retriever = retriever
        self._redactor = redactor

    def redact(self, text: str) -> Redacted:
        """The message with its personal data replaced, before anything else reads it.

        CPU-bound (spaCy): call it off the event loop. Raises `core.pii.DetectorUnavailable` if
        the detector fails, and the caller refuses the message: unredacted text goes no further.
        Without a redactor (tests, local runs) the text passes as it is; the API refuses to start
        that way on a cloud.
        """
        if self._redactor is None:
            return unredacted(text)
        # record_exception=False: a detector error can quote the text.
        with tracer.start_as_current_span(
            "chat.redaction", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("redaction.stage", "input")
            try:
                redacted = self._redactor.redact(text)
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", type(exc).__name__)
                raise
            span.set_attribute("redaction.count", sum(redacted.counts.values()))
            span.set_attribute("redaction.types", sorted(redacted.counts))
            return redacted

    async def reply(
        self, redacted: Redacted, market: str | None = None
    ) -> AsyncIterator[str | Sources]:
        """Streams the answer's text, after a `Sources` event when the market is retrieved from.

        Without a market or a retriever, the model answers alone. The market must already be a
        known one; the API refuses others before streaming.
        """
        message = redacted.text
        parts: tuple[str, ...] = PROMPT_PARTS
        prompt = message
        if market is not None and self._retriever is not None:
            retrieved = await self._retrieve(message, market)
            prompt, parts = user_message(message, retrieved), PROMPT_PARTS_WITH_CONTEXT
            yield sources_for(retrieved)

        tokens = TokenUsage()
        outcome: Outcome = "incomplete"  # stays so if the client leaves mid-stream
        ttft_ms: int | None = None
        blocked: tuple[str, ...] = ()
        # record_exception=False: the exception message can quote the user's text.
        with tracer.start_as_current_span(
            "chat.model", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("gen_ai.request.model", self._llm.model)
            span.set_attribute("chat.message_chars", len(message))
            start = time.perf_counter()
            chunks = 0
            try:
                async for delta in self._llm.stream(SYSTEM_PROMPT, prompt, usage=tokens):
                    if chunks == 0:
                        ttft_ms = round((time.perf_counter() - start) * 1000)
                        span.set_attribute("llm.time_to_first_token_ms", ttft_ms)
                    chunks += 1
                    yield delta
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
