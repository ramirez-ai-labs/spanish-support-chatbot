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


def gemini() -> LLMProvider:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        pytest.skip("GEMINI_API_KEY is not set")
    return GeminiLLM(api_key=key, model=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL))


ADAPTERS: list[Any] = [
    pytest.param(MockLLM, id="mock"),
    pytest.param(gemini, id="gemini", marks=pytest.mark.integration),
]


@pytest.fixture(params=ADAPTERS)
def llm(request: pytest.FixtureRequest) -> LLMProvider:
    build: Callable[[], LLMProvider] = request.param
    return build()


async def collect(stream: AsyncIterator[str | ToolRequest]) -> list[str]:
    """The streamed text. A tool request here fails the test: none was expected."""
    chunks = [chunk async for chunk in stream]
    assert all(isinstance(chunk, str) for chunk in chunks), "asked for a tool unprompted"
    return [chunk for chunk in chunks if isinstance(chunk, str)]


async def until_tools(stream: AsyncIterator[str | ToolRequest]) -> tuple[list[str], ToolRequest]:
    """The text before the tool request, and the request, which must come last."""
    chunks = [chunk async for chunk in stream]
    assert chunks and isinstance(chunks[-1], ToolRequest), "no tool request at the end"
    text = chunks[:-1]
    assert all(isinstance(chunk, str) for chunk in text), "a tool request before the end"
    return [chunk for chunk in text if isinstance(chunk, str)], chunks[-1]


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
    async def first_chunk_then_stop() -> str | ToolRequest:
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

    run(collect(llm.stream(SYSTEM_PROMPT, QUESTION, usage=alone)))
    chunks = run(collect(llm.stream(SYSTEM_PROMPT, QUESTION, usage=with_history, history=history)))

    assert "".join(chunks).strip()
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


# A synthetic card and its masked status, the shape the M3 tools return.
CARD_STATUS = ToolSpec(
    name="check_card_status",
    description="Consulta el estado de una tarjeta del cliente que inició sesión.",
    parameters={
        "type": "object",
        "properties": {
            "last4": {"type": "string", "description": "Últimos 4 dígitos de la tarjeta"}
        },
        "required": ["last4"],
    },
)
STATUS_QUESTION = "¿Cuál es el estado de mi tarjeta terminada en 4417?"
STATUS_RESULT = {"tarjeta": "terminada en 4417", "estado": "bloqueada"}

# Each adapter's own way to get a tool call: a script for the mock, and for Gemini a question only
# the offered tool can answer.
CALLING: list[Any] = [
    pytest.param(
        lambda: MockLLM(tool_steps=[[ToolCall("check_card_status", {"last4": "4417"})]]), id="mock"
    ),
    pytest.param(gemini, id="gemini", marks=pytest.mark.integration),
]


@pytest.fixture(params=CALLING)
def calling_llm(request: pytest.FixtureRequest) -> LLMProvider:
    build: Callable[[], LLMProvider] = request.param
    return build()


def test_asks_for_an_offered_tool_without_running_it(calling_llm: LLMProvider) -> None:
    _, request = run(
        until_tools(calling_llm.stream(SYSTEM_PROMPT, STATUS_QUESTION, tools=[CARD_STATUS]))
    )

    assert request.calls
    assert all(call.name == CARD_STATUS.name for call in request.calls)
    assert all(dict(call.arguments).get("last4") == "4417" for call in request.calls)


def test_answers_from_the_tool_results_it_gets_back(calling_llm: LLMProvider) -> None:
    # One event loop for both calls: the second sends back the turn the first returned.
    async def call_then_answer() -> tuple[list[str], TokenUsage, TokenUsage]:
        asked, answered = TokenUsage(), TokenUsage()
        _, request = await until_tools(
            calling_llm.stream(SYSTEM_PROMPT, STATUS_QUESTION, usage=asked, tools=[CARD_STATUS])
        )
        step = ToolStep(request, tuple(STATUS_RESULT for _ in request.calls))
        chunks = await collect(
            calling_llm.stream(
                SYSTEM_PROMPT, STATUS_QUESTION, usage=answered, tools=[CARD_STATUS], steps=[step]
            )
        )
        return chunks, asked, answered

    chunks, asked, answered = run(call_then_answer())

    assert "".join(chunks).strip()
    assert asked.output_tokens > 0  # a tool request is model output too
    assert answered.input_tokens > asked.input_tokens  # its turn and the results were sent


def test_offered_tools_alone_dont_force_a_call(llm: LLMProvider) -> None:
    chunks = run(collect(llm.stream(SYSTEM_PROMPT, "Hola, buenos días.", tools=[CARD_STATUS])))

    assert "".join(chunks).strip()


@pytest.mark.integration
def test_gemini_answers_with_what_the_tool_returned() -> None:
    llm = gemini()

    async def call_then_answer() -> str:
        _, request = await until_tools(
            llm.stream(SYSTEM_PROMPT, STATUS_QUESTION, tools=[CARD_STATUS])
        )
        step = ToolStep(request, tuple(STATUS_RESULT for _ in request.calls))
        chunks = await collect(
            llm.stream(SYSTEM_PROMPT, STATUS_QUESTION, tools=[CARD_STATUS], steps=[step])
        )
        return "".join(chunks)

    assert "bloquead" in run(call_then_answer()).lower()
