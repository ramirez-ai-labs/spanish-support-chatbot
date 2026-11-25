"""The agent loop in core.chat (ADR 0008): tools for signed-in callers, run by the core, capped.

The mock LLM asks for check_card_status when a message names the card ending 4417, as Gemini does
(the LLMProvider contract suite holds both to that). Synthetic cards and subs only.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import date
from typing import Any

import pytest

from adapters.shared.in_process_tools import InProcessToolProvider, Tool
from adapters.shared.memory_cards import MemoryCardStore
from adapters.shared.memory_usage import MemoryUsageSink
from adapters.shared.mock_llm import MockLLM
from core.cards import CHECK_CARD_STATUS, Card, check_card_status
from core.chat import MAX_STEPS, STEP_LIMIT_REPLY, ChatService
from core.identity import ANONYMOUS, Caller, Tier
from core.pii import PIISpan
from core.redaction import Redactor, unredacted
from core.sessions import Turn
from core.tools import ToolCall, ToolRequest, ToolSpec, ToolStep
from core.usage import TokenUsage

ALICE = Caller(Tier.SIGNED_IN, sub="test-sub-alice")
CARD_QUESTION = "¿Está activa mi tarjeta terminada en 4417?"
POLICY_QUESTION = "¿Cómo bloqueo mi tarjeta?"
STORE = MemoryCardStore(
    [("test-sub-alice", Card("4417", "debit", "active", date(2029, 12, 31), "US"))]
)
BLOCK_CARD = ToolSpec("block_card", "Bloquea una tarjeta.", action=True)


def card_tools(*extra: Tool) -> InProcessToolProvider:
    return InProcessToolProvider([Tool(CHECK_CARD_STATUS, check_card_status(STORE)), *extra])


def card_llm() -> MockLLM:
    return MockLLM(tool_script={"4417": ToolCall(CHECK_CARD_STATUS.name, {"last4": "4417"})})


def answer(service: ChatService, message: str, caller: Caller) -> str:
    async def consume() -> str:
        events = [e async for e in service.reply(unredacted(message), None, None, caller=caller)]
        return "".join(e for e in events if isinstance(e, str))

    return asyncio.run(consume())


class ScriptedLLM:
    """Asks for the scripted calls, one round at a time, then answers; or loops forever."""

    model = "scripted"

    def __init__(self, rounds: Sequence[Sequence[ToolCall]], loop: bool = False) -> None:
        self.rounds, self.loop = rounds, loop
        self.calls: list[tuple[int, tuple[ToolStep, ...]]] = []

    async def stream(
        self,
        system: str,
        user_message: str,
        usage: TokenUsage | None = None,
        history: Sequence[Turn] = (),
        tools: Sequence[ToolSpec] = (),
        steps: Sequence[ToolStep] = (),
    ) -> AsyncIterator[str | ToolRequest]:
        self.calls.append((len(tools), tuple(steps)))
        if usage is not None:
            usage.input_tokens = 10 * (len(steps) + 1)
        round_ = len(steps)
        if self.loop or round_ < len(self.rounds):
            calls = tuple(self.rounds[0 if self.loop else round_])
            yield "Un momento."
            yield ToolRequest(calls, object())
            return
        yield "Listo."


def test_a_signed_in_card_question_runs_the_tool_and_answers_with_its_result() -> None:
    llm, usage = card_llm(), MemoryUsageSink()
    service = ChatService(llm, usage=usage, tools=card_tools())

    text = answer(service, CARD_QUESTION, ALICE)

    (step,) = llm.last_steps
    assert step.results == ({"cards": [step.results[0]["cards"][0]]},)
    assert step.results[0]["cards"][0]["ending"] == "4417"
    assert '"ending": "4417"' in text  # the mock answers with the results it was given
    assert [r.prompt_parts for r in usage.records] == [
        ("system", "user", "tools"),
        ("system", "user", "tools", "tool_results"),
    ]
    assert all(r.outcome == "done" for r in usage.records)


def test_an_anonymous_caller_is_offered_no_tools_and_gets_one_call() -> None:
    llm, usage = card_llm(), MemoryUsageSink()
    service = ChatService(llm, usage=usage, tools=card_tools())

    answer(service, CARD_QUESTION, ANONYMOUS)

    assert llm.last_tools == ()
    assert [r.prompt_parts for r in usage.records] == [("system", "user")]


def test_a_question_that_needs_no_tool_is_still_one_call() -> None:
    llm, usage = card_llm(), MemoryUsageSink()
    service = ChatService(llm, usage=usage, tools=card_tools())

    answer(service, POLICY_QUESTION, ALICE)

    assert [t.name for t in llm.last_tools] == [CHECK_CARD_STATUS.name]
    assert len(usage.records) == 1 and llm.last_steps == ()


def test_the_loop_stops_at_the_step_cap() -> None:
    llm, usage = (
        ScriptedLLM([[ToolCall(CHECK_CARD_STATUS.name, {})]], loop=True),
        MemoryUsageSink(),
    )
    service = ChatService(llm, usage=usage, tools=card_tools())

    text = answer(service, CARD_QUESTION, ALICE)

    assert len(llm.calls) == MAX_STEPS  # never a sixth call
    assert len(llm.calls[-1][1]) == MAX_STEPS - 1  # and no tool round after the last one
    assert text.endswith(STEP_LIMIT_REPLY)


def test_the_loop_stops_once_the_input_token_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.chat.MAX_TURN_INPUT_TOKENS", 25)  # 10, then 10 + 20 = 30
    llm = ScriptedLLM([[ToolCall(CHECK_CARD_STATUS.name, {})]], loop=True)

    text = answer(ChatService(llm, tools=card_tools()), CARD_QUESTION, ALICE)

    assert len(llm.calls) == 2  # spent after the 2nd call, so no 3rd
    assert text.endswith(STEP_LIMIT_REPLY)


def test_a_tool_that_was_not_offered_never_runs() -> None:
    ran: list[str] = []

    async def block(caller: Caller, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        ran.append("block_card")
        return {"status": "blocked"}

    # Signed in, so block_card (an action) isn't offered; the model asks for it anyway.
    llm = ScriptedLLM([[ToolCall("block_card", {"last4": "4417"})]])
    answer(ChatService(llm, tools=card_tools(Tool(BLOCK_CARD, block))), CARD_QUESTION, ALICE)

    assert ran == []
    assert llm.calls[-1][1][0].results == ({"error": "unknown_tool"},)


def test_an_action_never_runs_from_the_loop_even_with_step_up() -> None:
    ran: list[str] = []

    async def block(caller: Caller, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        ran.append("block_card")
        return {"status": "blocked"}

    # With step-up the action is offered, but the loop never confirms: that's a button (ADR 0008).
    step_up = Caller(Tier.STEP_UP, sub="test-sub-alice")
    llm = ScriptedLLM([[ToolCall("block_card", {"last4": "4417"})]])
    answer(ChatService(llm, tools=card_tools(Tool(BLOCK_CARD, block))), CARD_QUESTION, step_up)

    assert ran == []
    assert llm.calls[-1][1][0].results == ({"error": "confirmation_required"},)


def test_a_failing_tool_tells_the_model_it_is_unavailable() -> None:
    class BrokenStore:
        def cards_of(self, sub: str) -> list[Card]:
            raise ConnectionError("db down")

    tools = InProcessToolProvider([Tool(CHECK_CARD_STATUS, check_card_status(BrokenStore()))])
    llm = card_llm()

    answer(ChatService(llm, tools=tools), CARD_QUESTION, ALICE)

    assert llm.last_steps[0].results == ({"error": "tool_unavailable"},)


def test_every_call_in_a_round_gets_its_own_result_in_order() -> None:
    calls = [ToolCall(CHECK_CARD_STATUS.name, {"last4": "4417"}), ToolCall("nope", {})]
    llm = ScriptedLLM([calls])

    answer(ChatService(llm, tools=card_tools()), CARD_QUESTION, ALICE)

    first, second = llm.calls[-1][1][0].results
    assert first["cards"][0]["ending"] == "4417"
    assert second == {"error": "unknown_tool"}


def test_text_from_two_rounds_reads_as_one_answer() -> None:
    llm = ScriptedLLM([[ToolCall(CHECK_CARD_STATUS.name, {})]])

    assert answer(ChatService(llm, tools=card_tools()), CARD_QUESTION, ALICE) == (
        "Un momento. Listo."
    )


class SSNDetector:
    """Flags one synthetic SSN, enough to see tool results go through the output scan."""

    SSN = "078-05-1120"  # the well-known specimen number, never issued

    def detect(self, text: str) -> list[PIISpan]:
        start = text.find(self.SSN)
        return [] if start == -1 else [PIISpan("US_SSN", start, start + len(self.SSN), 1.0)]

    def detect_patterns(self, text: str) -> list[PIISpan]:
        return self.detect(text)


def test_tool_results_are_scanned_before_the_model_reads_them() -> None:
    async def leaky(caller: Caller, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"note": f"SSN {SSNDetector.SSN}", "items": [SSNDetector.SSN], "count": 1}

    tool = ToolSpec("leaky_tool", "Devuelve datos.")
    llm = ScriptedLLM([[ToolCall("leaky_tool", {})]])
    service = ChatService(
        llm,
        redactor=Redactor(SSNDetector()),
        tools=InProcessToolProvider([Tool(tool, leaky)]),
    )

    answer(service, CARD_QUESTION, ALICE)

    (result,) = llm.calls[-1][1][0].results
    assert SSNDetector.SSN not in repr(result)
    assert result["count"] == 1
