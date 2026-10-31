"""Deterministic LLM for tests and CI, so they never call Gemini or need a key."""

from collections.abc import AsyncIterator

MOCK_REPLY = (
    "Para bloquear tu tarjeta, entra a la app del banco, abre la sección Tarjetas y elige "
    "«Bloquear tarjeta». Si no tienes acceso, puedo comunicarte con un agente."
)


class MockLLM:
    def __init__(self, reply: str = MOCK_REPLY) -> None:
        self._reply = reply

    async def stream(self, system: str, user_message: str) -> AsyncIterator[str]:
        for word in self._reply.split(" "):
            yield word + " "
