"""Deterministic LLM for tests and CI, so they never call Gemini or need a key."""

import json
from collections.abc import AsyncIterator, Mapping, Sequence
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
class _MockTurn:
    """The mock's own record of a turn that asked for tools, like Gemini's opaque Content."""

    calls: tuple[ToolCall, ...]


class MockLLM:
    """Streams a fixed reply, or asks for tools when scripted to.

    `tool_script` maps a trigger to a call: when the message contains the trigger, the call's tool
    is offered, and this turn has no tool rounds yet, the stream asks for it instead of answering.
    With rounds, it answers with the reply followed by the results it was given, so tests can see
    them arrive.
    """

    model = "mock"

    def __init__(
        self, reply: str = MOCK_REPLY, tool_script: Mapping[str, ToolCall] | None = None
    ) -> None:
        self._reply = reply
        self._script = dict(tool_script or {})
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
        self.last_history = tuple(history)
        self.last_tools = tuple(tools)
        self.last_steps = tuple(steps)
        if MOCK_BLOCK in user_message:
            raise ContentBlocked("output", ("TEST",))
        for step in steps:
            if not isinstance(step.request.turn, _MockTurn):
                raise TypeError("a ToolStep's turn must come from this adapter")
        offered = {spec.name for spec in tools}
        calls = tuple(
            call
            for trigger, call in self._script.items()
            if trigger in user_message and call.name in offered
        )
        results = [json.dumps(dict(r), ensure_ascii=False) for s in steps for r in s.results]
        reply = " ".join([self._reply, *results]) if steps else self._reply
        words = reply.split(" ")
        if usage is not None:  # deterministic stand-in counts: one token per word
            earlier = sum(len(t.question.split()) + len(t.answer.split()) for t in history)
            sent = sum(len(r.split()) for r in results)
            usage.input_tokens = len(system.split()) + earlier + len(user_message.split()) + sent
            usage.output_tokens = 1 if calls and not steps else len(words)
        if calls and not steps:
            yield ToolRequest(calls, _MockTurn(calls))
            return
        for word in words:
            yield word + " "
