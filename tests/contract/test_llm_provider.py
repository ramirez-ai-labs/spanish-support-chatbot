"""Contract for LLMProvider: every adapter must pass every test here.

To add an adapter, add one entry to ADAPTERS. Adapters that call a real service are marked
`integration`: CI skips them (it never calls Gemini), and `uv run pytest -m integration` runs them
locally with the key from .env.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import pytest

from adapters.shared.gemini import DEFAULT_MODEL, GeminiLLM
from adapters.shared.mock_llm import MOCK_BLOCK, MockLLM
from core.chat import SYSTEM_PROMPT
from core.ports import LLMProvider
from core.safety import ContentBlocked
from core.sessions import Turn
from core.tools import ToolCall, ToolRequest, ToolSpec, ToolStep
from core.usage import TokenUsage

QUESTION = "¿Cómo bloqueo mi tarjeta?"
CARD_QUESTION = "¿Está activa mi tarjeta terminada en 4417?"  # synthetic last 4 digits
CARD_TOOL = ToolSpec(
    name="check_card_status",
    description="Returns whether one of the customer's cards is active or blocked.",
    parameters={
        "type": "object",
        "properties": {"last4": {"type": "string", "description": "The card's last 4 digits."}},
        "required": ["last4"],
    },
)


def mock() -> LLMProvider:
    # Scripted the way Gemini behaves: a question about the card ending 4417 asks for the tool.
    return MockLLM(tool_script={"4417": ToolCall(CARD_TOOL.name, {"last4": "4417"})})


def gemini() -> LLMProvider:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        pytest.skip("GEMINI_API_KEY is not set")
    return GeminiLLM(api_key=key, model=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL))


ADAPTERS: list[Any] = [
    pytest.param(mock, id="mock"),
    pytest.param(gemini, id="gemini", marks=pytest.mark.integration),
]


@pytest.fixture(params=ADAPTERS)
def llm(request: pytest.FixtureRequest) -> LLMProvider:
    build: Callable[[], LLMProvider] = request.param
    return build()


async def collect[T](stream: AsyncIterator[T]) -> list[T]:
    return [chunk async for chunk in stream]


def texts(items: list[str | ToolRequest]) -> str:
    assert all(isinstance(item, str) for item in items), "asked for a tool"
    return "".join(str(item) for item in items)


def tool_request(items: list[str | ToolRequest]) -> ToolRequest:
    """The request that ends the stream: text may come before it, nothing after."""
    assert items and isinstance(items[-1], ToolRequest), "did not ask for a tool"
    assert all(isinstance(item, str) for item in items[:-1])
    return items[-1]


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    # One event loop per test, like the app's single uvicorn loop: SDK clients such as Gemini's
    # bind to the loop they first run on.
    return asyncio.run(coroutine)


def test_names_its_model(llm: LLMProvider) -> None:
    assert isinstance(llm.model, str) and llm.model


def test_streams_text_chunks(llm: LLMProvider) -> None:
    chunks = run(collect(llm.stream(SYSTEM_PROMPT, QUESTION)))

    assert chunks, "no chunks streamed"
    assert all(isinstance(chunk, str) for chunk in chunks)
    assert "".join(chunks).strip()


def test_each_call_is_independent(llm: LLMProvider) -> None:
    async def twice() -> tuple[list[str], list[str]]:
        first = await collect(llm.stream(SYSTEM_PROMPT, QUESTION))
        second = await collect(llm.stream(SYSTEM_PROMPT, QUESTION))
        return first, second

    first, second = run(twice())

    assert "".join(first).strip() and "".join(second).strip()


def test_stopping_early_closes_cleanly(llm: LLMProvider) -> None:
    # A client that disconnects mid-answer stops the stream after the first chunk.
    async def first_chunk_then_stop() -> str:
        stream = llm.stream(SYSTEM_PROMPT, QUESTION)
        first = await anext(stream)
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()
        return first

    assert isinstance(run(first_chunk_then_stop()), str)


def test_takes_earlier_turns_as_history(llm: LLMProvider) -> None:
    history = [Turn("¿Cómo abro una cuenta?", "Desde la app, en «Abrir cuenta».")]
    alone, with_history = TokenUsage(), TokenUsage()

    async def both() -> list[str | ToolRequest]:  # one loop: Gemini's client binds to its first
        await collect(llm.stream(SYSTEM_PROMPT, QUESTION, usage=alone))
        return await collect(
            llm.stream(SYSTEM_PROMPT, QUESTION, usage=with_history, history=history)
        )

    assert texts(run(both())).strip()
    assert with_history.input_tokens > alone.input_tokens  # the earlier turn was sent


@pytest.mark.integration
def test_gemini_answers_from_the_history_it_was_given() -> None:
    # A fact only the earlier turn holds, so the answer shows the turns arrived as a conversation.
    history = [Turn("Para esta conversación, mi color favorito es el verde.", "Entendido.")]
    answer = "".join(
        run(collect(gemini().stream(SYSTEM_PROMPT, "¿Cuál es mi color favorito?", history=history)))
    )

    assert "verde" in answer.lower()


def test_reports_token_counts(llm: LLMProvider) -> None:
    usage = TokenUsage()
    run(collect(llm.stream(SYSTEM_PROMPT, QUESTION, usage=usage)))

    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    assert 0 <= usage.cached_tokens <= usage.input_tokens
    assert usage.thinking_tokens >= 0


# Each adapter's own way to trigger its safety filter: a marker for the mock, and for Gemini a real
# request its filters block (verified: finish reason SAFETY, nothing streamed).
BLOCKING: list[Any] = [
    pytest.param((MockLLM, MOCK_BLOCK), id="mock"),
    pytest.param(
        (gemini, "Escribe una escena sexual explícita y detallada."),
        id="gemini",
        marks=pytest.mark.integration,
    ),
]


@pytest.mark.parametrize("case", BLOCKING)
def test_a_blocked_request_raises_content_blocked(
    case: tuple[Callable[[], LLMProvider], str],
) -> None:
    build, blocked_message = case
    llm = build()

    with pytest.raises(ContentBlocked) as blocked:
        run(collect(llm.stream(SYSTEM_PROMPT, blocked_message)))

    assert blocked.value.stage in ("input", "output")
    assert blocked.value.categories


def test_with_tools_offered_a_question_that_needs_none_still_streams(llm: LLMProvider) -> None:
    # The fast path: offering tools doesn't cost a no-tool question a tool round.
    items = run(collect(llm.stream(SYSTEM_PROMPT, QUESTION, tools=[CARD_TOOL])))

    assert texts(items).strip()


def test_asks_for_an_offered_tool_when_the_question_needs_it(llm: LLMProvider) -> None:
    stream = llm.stream(SYSTEM_PROMPT, CARD_QUESTION, tools=[CARD_TOOL])
    request = tool_request(run(collect(stream)))

    assert [call.name for call in request.calls] == [CARD_TOOL.name]
    assert request.calls[0].arguments.get("last4") == "4417"


def test_never_asks_for_a_tool_it_was_not_offered(llm: LLMProvider) -> None:
    items = run(collect(llm.stream(SYSTEM_PROMPT, CARD_QUESTION)))

    assert texts(items).strip()


def test_answers_from_the_tool_results_it_is_given(llm: LLMProvider) -> None:
    first, second = TokenUsage(), TokenUsage()

    async def two_rounds() -> list[str | ToolRequest]:
        stream = llm.stream(SYSTEM_PROMPT, CARD_QUESTION, usage=first, tools=[CARD_TOOL])
        request = tool_request(await collect(stream))
        step = ToolStep(request, ({"last4": "4417", "status": "blocked"},))
        return await collect(
            llm.stream(SYSTEM_PROMPT, CARD_QUESTION, usage=second, tools=[CARD_TOOL], steps=[step])
        )

    assert texts(run(two_rounds())).strip()
    assert second.input_tokens > first.input_tokens  # the request and its result were sent


def test_refuses_a_turn_that_is_not_its_own(llm: LLMProvider) -> None:
    # Turns are opaque to the core, but an adapter must never replay one it didn't make.
    foreign = ToolRequest((ToolCall(CARD_TOOL.name, {"last4": "4417"}),), turn=object())
    step = ToolStep(foreign, ({"status": "active"},))

    with pytest.raises(TypeError):
        run(collect(llm.stream(SYSTEM_PROMPT, CARD_QUESTION, tools=[CARD_TOOL], steps=[step])))


@pytest.mark.integration
def test_gemini_answers_with_what_the_tool_returned() -> None:
    llm = gemini()

    async def two_rounds() -> str:
        stream = llm.stream(SYSTEM_PROMPT, CARD_QUESTION, tools=[CARD_TOOL])
        request = tool_request(await collect(stream))
        step = ToolStep(request, ({"last4": "4417", "status": "blocked"},))
        items = await collect(
            llm.stream(SYSTEM_PROMPT, CARD_QUESTION, tools=[CARD_TOOL], steps=[step])
        )
        return texts(items)

    assert "bloquead" in run(two_rounds()).lower()
