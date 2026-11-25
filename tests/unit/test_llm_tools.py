"""The LLMProvider's tool-calling pieces: the request Gemini gets, the mock's script, and the chat
service failing closed while no tools are offered. Behavior shared by adapters is in the contract
suite."""

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest
from google.genai import types

from adapters.shared.gemini import contents, declarations
from adapters.shared.memory_usage import MemoryUsageSink
from adapters.shared.mock_llm import MockLLM, MockTurn
from core.chat import SYSTEM_PROMPT, ChatService, UnexpectedToolRequest
from core.redaction import unredacted
from core.sessions import Turn
from core.tools import ToolCall, ToolRequest, ToolSpec, ToolStep
from core.usage import TokenUsage

STATUS = ToolSpec(
    "check_card_status",
    "Estado de una tarjeta",
    parameters={"type": "object", "properties": {"last4": {"type": "string"}}},
)
CALL = ToolCall("check_card_status", {"last4": "4417"})


async def drain(stream: AsyncIterator[str | ToolRequest]) -> list[str | ToolRequest]:
    return [chunk async for chunk in stream]


def test_a_step_needs_one_result_per_call() -> None:
    request = ToolRequest(calls=(CALL, CALL), turn=object())

    with pytest.raises(ValueError):
        ToolStep(request, ({"estado": "activa"},))


# Gemini


def test_no_tools_declares_none_as_before() -> None:
    assert declarations([]) is None


def test_tools_are_declared_with_their_schema_and_an_empty_one_is_left_out() -> None:
    bare = ToolSpec("open_ticket", "Abre un caso", action=True)

    (tool,) = declarations([STATUS, bare]) or []
    status, ticket = tool.function_declarations or []

    assert (status.name, status.parameters_json_schema) == ("check_card_status", STATUS.parameters)
    assert (ticket.name, ticket.parameters_json_schema) == ("open_ticket", None)


def test_a_step_sends_the_models_turn_unchanged_then_each_result_by_call_id() -> None:
    signed = types.Part(
        function_call=types.FunctionCall(id="call-1", name=CALL.name, args={"last4": "4417"}),
        thought_signature=b"opaque",
    )
    turn = types.Content(role="model", parts=[signed])
    step = ToolStep(ToolRequest((CALL,), turn), ({"estado": "activa"},))

    sent = contents("¿Y mi tarjeta?", [Turn("Hola", "¡Hola!")], [step])

    assert [c.role for c in sent] == ["user", "model", "user", "model", "user"]
    assert sent[3] is turn  # thought signature and all
    (answer,) = sent[4].parts or []
    assert answer.function_response is not None
    assert (answer.function_response.id, answer.function_response.name) == ("call-1", CALL.name)
    assert answer.function_response.response == {"estado": "activa"}


def test_a_turn_from_another_provider_is_refused() -> None:
    step = ToolStep(ToolRequest((CALL,), MockTurn(0)), ({"estado": "activa"},))

    with pytest.raises(TypeError):
        contents("¿Y mi tarjeta?", [], [step])


# The mock


def test_the_mock_asks_for_its_script_then_answers() -> None:
    llm = MockLLM(tool_steps=[[CALL]])

    first = asyncio.run(drain(llm.stream(SYSTEM_PROMPT, "¿Y mi tarjeta?", tools=[STATUS])))
    (request,) = first
    assert isinstance(request, ToolRequest) and request.calls == (CALL,)

    step = ToolStep(request, ({"estado": "activa"},))
    second = asyncio.run(
        drain(llm.stream(SYSTEM_PROMPT, "¿Y mi tarjeta?", tools=[STATUS], steps=[step]))
    )
    assert second and all(isinstance(chunk, str) for chunk in second)
    assert llm.last_steps == (step,)


def test_the_mock_never_calls_a_tool_that_isnt_offered() -> None:
    llm = MockLLM(tool_steps=[[CALL]])

    without = asyncio.run(drain(llm.stream(SYSTEM_PROMPT, "¿Y mi tarjeta?")))
    assert all(isinstance(chunk, str) for chunk in without)  # no tools offered: it just answers

    other = ToolSpec("open_ticket", "Abre un caso", action=True)
    with pytest.raises(ValueError):
        asyncio.run(drain(llm.stream(SYSTEM_PROMPT, "¿Y mi tarjeta?", tools=[other])))


def test_the_mock_refuses_a_turn_that_was_changed() -> None:
    step = ToolStep(ToolRequest((CALL,), MockTurn(3)), ({"estado": "activa"},))

    with pytest.raises(ValueError):
        asyncio.run(drain(MockLLM().stream(SYSTEM_PROMPT, "Hola", tools=[STATUS], steps=[step])))


# The chat service, which offers no tools until the agent loop


class AsksForATool:
    model = "rogue"

    async def stream(
        self,
        system: str,
        user_message: str,
        usage: TokenUsage | None = None,
        history: Sequence[Turn] = (),
        tools: Sequence[ToolSpec] = (),
        steps: Sequence[ToolStep] = (),
    ) -> AsyncIterator[str | ToolRequest]:
        yield ToolRequest((CALL,), turn=object())


def test_the_chat_offers_no_tools_yet() -> None:
    llm = MockLLM()

    async def consume() -> None:
        async for _ in ChatService(llm).reply(unredacted("Hola")):
            pass

    asyncio.run(consume())
    assert llm.last_tools == () and llm.last_steps == ()


def test_a_tool_request_nobody_offered_fails_the_answer_and_is_recorded() -> None:
    sink = MemoryUsageSink()

    async def consume() -> None:
        async for _ in ChatService(AsksForATool(), usage=sink).reply(unredacted("Hola")):
            pass

    with pytest.raises(UnexpectedToolRequest):
        asyncio.run(consume())
    (record,) = sink.records
    assert record.outcome == "error"
