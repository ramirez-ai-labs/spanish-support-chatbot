"""Deterministic LLM for tests and CI, so they never call Gemini or need a key."""

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from core.safety import ContentBlocked
from core.sessions import Turn
from core.tools import ToolCall, ToolRequest, ToolSpec, ToolStep
from core.usage import TokenUsage

# A message containing this is "blocked", so tests can exercise the refusal path without a model.
MOCK_BLOCK = "[[mock-block]]"

MOCK_REPLY = (
    "Para bloquear tu tarjeta, entra a la app del banco, abre la sección Tarjetas y elige "
    "«Bloquear tarjeta». Si no tienes acceso, puedo comunicarte con un agente."
)


@dataclass(frozen=True)
class MockTurn:
    """The mock's opaque turn, so tests can check the core passed it back unchanged."""

    step: int


class MockLLM:
    """Answers with a fixed reply, or asks for scripted tools first.

    `tool_steps[i]` are the calls the mock asks for at step i (when given i earlier steps), as long
    as tools are offered; after the script it answers in text. Scripting a tool that isn't offered
    is a test bug, so it raises instead of pretending.
    """

    model = "mock"

    def __init__(
        self, reply: str = MOCK_REPLY, tool_steps: Sequence[Sequence[ToolCall]] = ()
    ) -> None:
        self._reply = reply
        self._tool_steps = [tuple(calls) for calls in tool_steps]
        # What the latest call was given, for tests.
        self.last_history: tuple[Turn, ...] = ()
        self.last_tools: tuple[ToolSpec, ...] = ()
        self.last_steps: tuple[ToolStep, ...] = ()

    async def stream(
        self,
        system: str,
        user_message: str,
        usage: TokenUsage | None = None,
        history: Sequence[Turn] = (),
        tools: Sequence[ToolSpec] = (),
        steps: Sequence[ToolStep] = (),
    ) -> AsyncIterator[str | ToolRequest]:
        self.last_history, self.last_tools, self.last_steps = (
            tuple(history),
            tuple(tools),
            tuple(steps),
        )
        if MOCK_BLOCK in user_message:
            raise ContentBlocked("output", ("TEST",))
        for i, step in enumerate(steps):
            if step.request.turn != MockTurn(i):
                raise ValueError("A tool step's turn must come back exactly as the model sent it")
        calls = self._tool_steps[len(steps)] if tools and len(steps) < len(self._tool_steps) else ()
        offered = {spec.name for spec in tools}
        if any(call.name not in offered for call in calls):
            raise ValueError("The mock was scripted to call a tool that wasn't offered")
        words = [] if calls else self._reply.split(" ")
        if usage is not None:  # deterministic stand-in counts: one token per word
            earlier = sum(len(t.question.split()) + len(t.answer.split()) for t in history)
            declared = sum(
                len(json.dumps([s.name, s.description, s.parameters]).split()) for s in tools
            )
            results = sum(len(json.dumps(r).split()) for step in steps for r in step.results)
            usage.input_tokens = (
                len(system.split()) + earlier + len(user_message.split()) + declared + results
            )
            usage.output_tokens = len(words) + len(calls)
        for word in words:
            yield word + " "
        if calls:
            yield ToolRequest(calls=calls, turn=MockTurn(len(steps)))
