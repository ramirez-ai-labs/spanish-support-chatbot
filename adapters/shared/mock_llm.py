"""Deterministic LLM for tests and CI, so they never call Gemini or need a key."""

from collections.abc import AsyncIterator

from core.usage import TokenUsage

MOCK_REPLY = (
    "Para bloquear tu tarjeta, entra a la app del banco, abre la sección Tarjetas y elige "
    "«Bloquear tarjeta». Si no tienes acceso, puedo comunicarte con un agente."
)


class MockLLM:
    model = "mock"

    def __init__(self, reply: str = MOCK_REPLY) -> None:
        self._reply = reply

    async def stream(
        self, system: str, user_message: str, usage: TokenUsage | None = None
    ) -> AsyncIterator[str]:
        words = self._reply.split(" ")
        if usage is not None:  # deterministic stand-in counts: one token per word
            usage.input_tokens = len(system.split()) + len(user_message.split())
            usage.output_tokens = len(words)
        for word in words:
            yield word + " "
