import json
import logging
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from adapters.shared.mock_llm import MOCK_REPLY, MockLLM
from apps.api.main import build_llm, create_app
from core.chat import SYSTEM_PROMPT


def parse_sse(body: str) -> list[tuple[str, dict[str, str]]]:
    events = []
    for block in body.strip().split("\n\n"):
        name, data = "message", "{}"
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
        events.append((name, json.loads(data)))
    return events


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(llm=MockLLM()))


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_chat_streams_deltas_then_done(client: TestClient) -> None:
    response = client.post("/v1/chat", json={"message": "¿Cómo bloqueo mi tarjeta?"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert events[-1][0] == "done"
    text = "".join(data["delta"] for name, data in events if name == "message")
    assert text.strip() == MOCK_REPLY


def test_chat_keeps_spanish_characters_unescaped(client: TestClient) -> None:
    body = client.post("/v1/chat", json={"message": "hola"}).text
    assert "sección" in body


@pytest.mark.parametrize("message", ["", "x" * 2001])
def test_chat_rejects_empty_or_oversized_message(client: TestClient, message: str) -> None:
    assert client.post("/v1/chat", json={"message": message}).status_code == 422


def test_provider_error_becomes_generic_error_event() -> None:
    secret = "provider detail with user text"

    class FailingLLM:
        model = "fake"

        async def stream(self, system: str, user_message: str) -> AsyncIterator[str]:
            raise RuntimeError(secret)
            yield ""

    body = TestClient(create_app(llm=FailingLLM())).post("/v1/chat", json={"message": "hola"}).text

    assert parse_sse(body) == [("error", {"error": "model_unavailable"})]
    assert secret not in body


def test_provider_error_logs_type_and_code_only(caplog: pytest.LogCaptureFixture) -> None:
    secret = "API key not valid: hola"

    class ProviderError(Exception):
        code = 401

    class FailingLLM:
        model = "fake"

        async def stream(self, system: str, user_message: str) -> AsyncIterator[str]:
            raise ProviderError(secret)
            yield ""

    with caplog.at_level(logging.WARNING, logger="apps.api.main"):
        TestClient(create_app(llm=FailingLLM())).post("/v1/chat", json={"message": "hola"})

    assert "llm_error type=ProviderError code=401" in caplog.text
    assert secret not in caplog.text


def test_system_prompt_is_passed_to_provider() -> None:
    seen: list[str] = []

    class RecordingLLM:
        model = "fake"

        async def stream(self, system: str, user_message: str) -> AsyncIterator[str]:
            seen.append(system)
            yield "ok"

    TestClient(create_app(llm=RecordingLLM())).post("/v1/chat", json={"message": "hola"})

    assert seen == [SYSTEM_PROMPT]


def test_build_llm_selects_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    assert isinstance(build_llm(), MockLLM)


def test_build_llm_requires_gemini_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        build_llm()


def test_build_llm_rejects_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "nope")
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        build_llm()
