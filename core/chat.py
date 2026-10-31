"""Chat orchestration. M0: one model call, streamed. Retrieval, guardrails and tools come later."""

from collections.abc import AsyncIterator

from core.ports import LLMProvider

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

    def reply(self, message: str) -> AsyncIterator[str]:
        return self._llm.stream(SYSTEM_PROMPT, message)
