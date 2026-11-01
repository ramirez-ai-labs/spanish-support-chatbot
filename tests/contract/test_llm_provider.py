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
from adapters.shared.mock_llm import MockLLM
from core.chat import SYSTEM_PROMPT
from core.ports import LLMProvider

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


async def collect(stream: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in stream]


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
