import asyncio

import pytest
from fastapi.testclient import TestClient
from google.genai import types

from adapters.shared.gemini import SAFETY_SETTINGS, _flagged_categories
from adapters.shared.memory_usage import MemoryUsageSink
from adapters.shared.mock_llm import MOCK_BLOCK, MockLLM
from apps.api.main import create_app
from core.chat import ChatService
from core.redaction import unredacted
from core.safety import ContentBlocked

MESSAGE = f"texto privado que no debe guardarse {MOCK_BLOCK}"


def test_a_blocked_answer_is_recorded_with_its_category_and_no_text() -> None:
    sink = MemoryUsageSink()

    async def consume() -> None:
        async for _ in ChatService(MockLLM(), usage=sink).reply(unredacted(MESSAGE)):
            pass

    with pytest.raises(ContentBlocked):
        asyncio.run(consume())

    (record,) = sink.records
    assert record.outcome == "blocked"
    assert record.blocked_categories == ("TEST",)
    assert "texto privado" not in repr(record)


def test_the_api_answers_a_block_with_a_refusal(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="apps.api.main"):
        body = (
            TestClient(create_app(llm=MockLLM()))
            .post("/v1/chat", json={"message": MESSAGE, "market": "US"})
            .text
        )

    assert 'event: error\ndata: {"error": "content_blocked"}' in body
    assert "content_blocked stage=output categories=TEST" in caplog.text
    assert "texto privado" not in caplog.text


def test_every_category_uses_the_strictest_threshold() -> None:
    assert {s.category for s in SAFETY_SETTINGS} == {
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    }
    assert {s.threshold for s in SAFETY_SETTINGS} == {types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE}


def test_flagged_categories_are_short_names_of_blocked_or_rated_categories() -> None:
    candidate = types.Candidate(
        finish_reason=types.FinishReason.SAFETY,
        safety_ratings=[
            types.SafetyRating(
                category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                probability=types.HarmProbability.MEDIUM,
                blocked=True,
            ),
            types.SafetyRating(
                category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                probability=types.HarmProbability.NEGLIGIBLE,
            ),
        ],
    )
    assert _flagged_categories(candidate) == ("SEXUALLY_EXPLICIT",)
