"""Deterministic LLM for tests and CI, so they never call Gemini or need a key."""

from collections.abc import AsyncIterator, Sequence

from core.safety import ContentBlocked
from core.sessions import Turn
from core.usage import TokenUsage

# A message containing this is "blocked", so tests can exercise the refusal path without a model.
MOCK_BLOCK = "[[mock-block]]"

MOCK_REPLY = (
    "Para bloquear tu tarjeta, entra a la app del banco, abre la sección Tarjetas y elige "
    "«Bloquear tarjeta». Si no tienes acceso, puedo comunicarte con un agente."
)


class MockLLM:
    model = "mock"

    def __init__(self, reply: str = MOCK_REPLY) -> None:
        self._reply = reply
        self.last_history: tuple[Turn, ...] = ()  # what the latest call was given, for tests

    async def stream(
        self,
        system: str,
        user_message: str,
        usage: TokenUsage | None = None,
        history: Sequence[Turn] = (),
    ) -> AsyncIterator[str]:
        self.last_history = tuple(history)
        if MOCK_BLOCK in user_message:
            raise ContentBlocked("output", ("TEST",))
        words = self._reply.split(" ")
        if usage is not None:  # deterministic stand-in counts: one token per word
            earlier = sum(len(t.question.split()) + len(t.answer.split()) for t in history)
            usage.input_tokens = len(system.split()) + earlier + len(user_message.split())
            usage.output_tokens = len(words)
        for word in words:
            yield word + " "
