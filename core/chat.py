"""Chat orchestration. M0: one model call, streamed. Retrieval, guardrails and tools come later.

Each step is one span (OpenTelemetry API only, which is cloud-free), so time to first token breaks
down by stage. Spans carry sizes and timings, never message text: redaction only arrives in M3.
"""

import time
from collections.abc import AsyncIterator

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from core.ports import LLMProvider

tracer = trace.get_tracer(__name__)

# Kept identical across calls so the prefix can be cached once prompt caching is wired in.
SYSTEM_PROMPT = (
    "Eres el asistente de soporte de un banco ficticio para clientes hispanohablantes. "
    "Responde siempre en español, de forma breve, clara y amable. "
    "Si no sabes algo o la pregunta requiere acceso a la cuenta del cliente, dilo y ofrece "
    "comunicar al cliente con un agente humano. "
    "Nunca pidas números completos de tarjeta, contraseñas ni documentos de identidad."
)

MAX_MESSAGE_CHARS = 2000


class ChatService:
    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    async def reply(self, message: str) -> AsyncIterator[str]:
        # record_exception=False: the exception message can quote the user's text.
        with tracer.start_as_current_span(
            "chat.model", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("gen_ai.request.model", self._llm.model)
            span.set_attribute("chat.message_chars", len(message))
            start = time.perf_counter()
            chunks = 0
            try:
                async for delta in self._llm.stream(SYSTEM_PROMPT, message):
                    if chunks == 0:
                        ttft_ms = round((time.perf_counter() - start) * 1000)
                        span.set_attribute("llm.time_to_first_token_ms", ttft_ms)
                    chunks += 1
                    yield delta
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", type(exc).__name__)
                raise
            finally:
                span.set_attribute("llm.output_chunks", chunks)
