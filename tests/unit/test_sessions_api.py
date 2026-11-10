"""Sessions in the chat API (ADR 0008): the ID goes to the page in a `session` event, and comes back
with the next message. All values are synthetic."""

import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from adapters.shared.memory_sessions import MemorySessionStore
from adapters.shared.mock_llm import MockLLM
from apps.api.main import build_sessions, create_app
from core.sessions import Sessions

KEY = "ab" * 32  # synthetic


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
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


def chat(client: TestClient, message: str, session_id: str | None = None) -> list[Any]:
    body: dict[str, Any] = {"message": message, "market": "US"}
    if session_id is not None:
        body["session_id"] = session_id
    response = client.post("/v1/chat", json=body)
    assert response.status_code == 200
    return parse_sse(response.text)


@pytest.fixture
def llm() -> MockLLM:
    return MockLLM()


@pytest.fixture
def client(llm: MockLLM) -> TestClient:
    sessions = Sessions(MemorySessionStore(), bytes.fromhex(KEY))
    return TestClient(create_app(llm=llm, sessions=sessions))


def test_the_session_comes_first_and_carries_the_conversation(
    client: TestClient, llm: MockLLM
) -> None:
    first = chat(client, "¿Cómo bloqueo mi tarjeta?")
    name, data = first[0]
    assert name == "session" and len(data["session_id"]) == 32
    assert first[-1][0] == "done"

    second = chat(client, "¿Y si la encuentro?", data["session_id"])

    assert second[0] == ("session", data)  # the same conversation
    [earlier] = llm.last_history
    assert earlier.question == "¿Cómo bloqueo mi tarjeta?"


def test_an_unknown_id_gets_a_new_conversation(client: TestClient, llm: MockLLM) -> None:
    events = chat(client, "Hola", "f" * 32)

    assert events[0][0] == "session" and events[0][1]["session_id"] != "f" * 32
    assert llm.last_history == ()


def test_an_oversized_id_is_refused_like_any_bad_field(client: TestClient) -> None:
    body = {"message": "Hola", "market": "US", "session_id": "a" * 65}
    assert client.post("/v1/chat", json=body).status_code == 422


def test_without_sessions_there_is_no_session_event() -> None:
    events = chat(TestClient(create_app(llm=MockLLM())), "Hola")
    assert [name for name, _ in events if name == "session"] == []


def test_the_id_never_reaches_the_logs(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        first = chat(client, "Hola")
        chat(client, "Otra vez", first[0][1]["session_id"])

    assert first[0][1]["session_id"] not in caplog.text


# --- configuration -------------------------------------------------------------------------------


def test_sessions_are_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SESSION_STORE", raising=False)
    assert build_sessions() is None


def test_memory_sessions_make_their_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.delenv("SESSION_HMAC_KEY", raising=False)
    assert isinstance(build_sessions(), Sessions)


@pytest.mark.parametrize(
    "key, error",
    [("", "32 bytes"), ("ab" * 16, "32 bytes"), ("not-hex-" * 8, "must be hex")],
    ids=["missing", "short", "not-hex"],
)
def test_firestore_sessions_need_the_whole_key(
    monkeypatch: pytest.MonkeyPatch, key: str, error: str
) -> None:
    monkeypatch.setenv("SESSION_STORE", "firestore")
    monkeypatch.setenv("SESSION_HMAC_KEY", key)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-sessions")
    with pytest.raises(ValueError, match=error) as raised:
        build_sessions()
    if key:
        assert key not in str(raised.value)  # the error names the variable, never its value


def test_an_unknown_store_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_STORE", "redis")
    with pytest.raises(ValueError, match="Unknown SESSION_STORE"):
        build_sessions()
