"""Chat orchestration. M0: one model call, streamed. Retrieval, guardrails and tools come later.

Each step is one span (OpenTelemetry API only, which is cloud-free), so time to first token breaks
down by stage. Spans carry sizes and timings, never message text: redaction only arrives in M3.
Every model call also writes one usage record, after the answer has streamed.
"""

import asyncio
import time
from collections.abc import AsyncIterator

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from core.ports import LLMProvider, UsageSink
from core.usage import LLMUsage, Outcome, TokenUsage

tracer = trace.get_tracer(__name__)

# Kept identical across calls so the prefix can be cached once prompt caching is wired in.
SYSTEM_PROMPT = (
    "Eres el asistente de soporte de un banco ficticio para clientes hispanohablantes. "
    "Responde siempre en español, de forma breve, clara y amable. "
    "Si no sabes algo o la pregunta requiere acceso a la cuenta del cliente, dilo y ofrece "
    "comunicar al cliente con un agente humano. "
    "Nunca pidas números completos de tarjeta, contraseñas ni documentos de identidad."
)

# What the prompt is made of, recorded by name (never content) with each call. Retrieval adds
# "chunks" in M2 and sessions add "history" in M3.
PROMPT_PARTS = ("system", "user")

MAX_MESSAGE_CHARS = 2000


class ChatService:
    def __init__(self, llm: LLMProvider, usage: UsageSink | None = None, cloud: str = "local"):
        self._llm = llm
        self._usage = usage
        self._cloud = cloud

    async def reply(self, message: str) -> AsyncIterator[str]:
        tokens = TokenUsage()
        outcome: Outcome = "incomplete"  # stays so if the client leaves mid-stream
        ttft_ms: int | None = None
        # record_exception=False: the exception message can quote the user's text.
        with tracer.start_as_current_span(
            "chat.model", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("gen_ai.request.model", self._llm.model)
            span.set_attribute("chat.message_chars", len(message))
            start = time.perf_counter()
            chunks = 0
            try:
                async for delta in self._llm.stream(SYSTEM_PROMPT, message, usage=tokens):
                    if chunks == 0:
                        ttft_ms = round((time.perf_counter() - start) * 1000)
                        span.set_attribute("llm.time_to_first_token_ms", ttft_ms)
                    chunks += 1
                    yield delta
                outcome = "done"
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
                        prompt_parts=PROMPT_PARTS,
                        tokens=tokens,
                        latency_ms=round((time.perf_counter() - start) * 1000),
                        ttft_ms=ttft_ms,
                        outcome=outcome,
                        trace_id=f"{context.trace_id:032x}" if context.is_valid else None,
                    )
                    # Shielded: if the client disconnects and the request is cancelled, the row
                    # is still written.
                    await asyncio.shield(self._usage.record(record))
