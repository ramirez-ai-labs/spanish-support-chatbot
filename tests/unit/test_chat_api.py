import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from adapters.shared.hash_embedder import HashEmbedder
from adapters.shared.mock_llm import MOCK_REPLY, MockLLM
from apps.api.main import KNOWLEDGE, build_llm, build_retriever, client_timing, create_app
from core.chat import SYSTEM_PROMPT
from core.knowledge import load_markets
from core.retrieval import RetrievedChunk, Retriever

MARKETS = load_markets(KNOWLEDGE / "markets.yaml")


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


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(llm=MockLLM()))


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_chat_streams_deltas_then_done(client: TestClient) -> None:
    response = client.post(
        "/v1/chat", json={"message": "¿Cómo bloqueo mi tarjeta?", "market": "US"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert events[-1][0] == "done"
    text = "".join(data["delta"] for name, data in events if name == "message")
    assert text.strip() == MOCK_REPLY


def test_chat_keeps_spanish_characters_unescaped(client: TestClient) -> None:
    body = client.post("/v1/chat", json={"message": "hola", "market": "US"}).text
    assert "sección" in body


@pytest.mark.parametrize("message", ["", "x" * 2001])
def test_chat_rejects_empty_or_oversized_message(client: TestClient, message: str) -> None:
    assert client.post("/v1/chat", json={"message": message, "market": "US"}).status_code == 422


def test_provider_error_becomes_generic_error_event() -> None:
    secret = "provider detail with user text"

    class FailingLLM:
        model = "fake"

        async def stream(
            self, system: str, user_message: str, usage: object = None, history: object = ()
        ) -> AsyncIterator[str]:
            raise RuntimeError(secret)
            yield ""

    body = (
        TestClient(create_app(llm=FailingLLM()))
        .post("/v1/chat", json={"message": "hola", "market": "US"})
        .text
    )

    assert parse_sse(body) == [("error", {"error": "model_unavailable"})]
    assert secret not in body


def test_provider_error_logs_type_and_code_only(caplog: pytest.LogCaptureFixture) -> None:
    secret = "API key not valid: hola"

    class ProviderError(Exception):
        code = 401

    class FailingLLM:
        model = "fake"

        async def stream(
            self, system: str, user_message: str, usage: object = None, history: object = ()
        ) -> AsyncIterator[str]:
            raise ProviderError(secret)
            yield ""

    with caplog.at_level(logging.WARNING, logger="apps.api.main"):
        TestClient(create_app(llm=FailingLLM())).post(
            "/v1/chat", json={"message": "hola", "market": "US"}
        )

    assert "llm_error type=ProviderError code=401" in caplog.text
    assert secret not in caplog.text


def test_system_prompt_is_passed_to_provider() -> None:
    seen: list[str] = []

    class RecordingLLM:
        model = "fake"

        async def stream(
            self, system: str, user_message: str, usage: object = None, history: object = ()
        ) -> AsyncIterator[str]:
            seen.append(system)
            yield "ok"

    TestClient(create_app(llm=RecordingLLM())).post(
        "/v1/chat", json={"message": "hola", "market": "US"}
    )

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


@pytest.fixture
def rag(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The real knowledge docs indexed in memory, as local runs do, with the hash embedder."""
    monkeypatch.setenv("KNOWLEDGE_STORE", "memory")
    return TestClient(create_app(llm=MockLLM(), retriever=build_retriever(HashEmbedder(), MARKETS)))


def test_markets_are_served_from_config(client: TestClient) -> None:
    assert client.get("/v1/markets").json() == {
        "markets": [
            {"code": "US", "name": "Estados Unidos"},
            {"code": "MX", "name": "México"},
            {"code": "SV", "name": "El Salvador"},
            {"code": "ES", "name": "España"},
        ]
    }


@pytest.mark.parametrize("market", ["CO", "mx", ""])
def test_an_unknown_market_is_refused_before_anything_streams(rag: TestClient, market: str) -> None:
    response = rag.post("/v1/chat", json={"message": "hola", "market": market})
    assert (response.status_code, response.json()) == (400, {"error": "unknown_market"})


def test_sources_arrive_before_the_answer_and_end_with_done(rag: TestClient) -> None:
    events = parse_sse(
        rag.post("/v1/chat", json={"message": "¿Cómo bloqueo mi tarjeta?", "market": "MX"}).text
    )
    names = [name for name, _ in events]
    assert names[0] == "sources" and names[-1] == "done" and "message" in names
    sources = events[0][1]["sources"]
    assert sources and set(sources[0]) == {"title", "section", "market"}  # no IDs or scores


def test_the_same_question_in_two_markets_cites_different_policies(rag: TestClient) -> None:
    def sources(market: str) -> list[dict[str, str]]:
        body = rag.post("/v1/chat", json={"message": "Comisiones de la cuenta", "market": market})
        return parse_sse(body.text)[0][1]["sources"]

    us, mx = sources("US"), sources("MX")
    assert {s["market"] for s in us} <= {"US", "all"}
    assert {s["market"] for s in mx} <= {"MX", "all"}
    assert us != mx


def test_a_message_without_a_market_is_refused_before_anything_streams(rag: TestClient) -> None:
    response = rag.post("/v1/chat", json={"message": "hola"})
    assert (response.status_code, response.json()) == (400, {"error": "market_required"})


def test_a_failed_retrieval_becomes_a_generic_error_event(caplog: pytest.LogCaptureFixture) -> None:
    class BrokenStore:
        def search(
            self, vector: Sequence[float], markets: Sequence[str], k: int, model: str
        ) -> list[RetrievedChunk]:
            raise ConnectionError("detail with user text")

    retriever = Retriever(HashEmbedder(), BrokenStore(), MARKETS)
    client = TestClient(create_app(llm=MockLLM(), retriever=retriever))
    with caplog.at_level(logging.WARNING):
        body = client.post("/v1/chat", json={"message": "hola", "market": "US"}).text

    assert parse_sse(body) == [("error", {"error": "knowledge_unavailable"})]
    assert "knowledge_error type=ConnectionError" in caplog.text
    assert "detail with user text" not in caplog.text


def test_build_retriever_is_off_by_default_and_needs_an_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KNOWLEDGE_STORE", raising=False)
    assert build_retriever(HashEmbedder(), MARKETS) is None
    monkeypatch.setenv("KNOWLEDGE_STORE", "memory")
    with pytest.raises(ValueError, match="needs an EMBEDDER"):
        build_retriever(None, MARKETS)
    monkeypatch.setenv("KNOWLEDGE_STORE", "nope")
    with pytest.raises(ValueError, match="Unknown KNOWLEDGE_STORE"):
        build_retriever(HashEmbedder(), MARKETS)


def test_the_score_threshold_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_STORE", "memory")
    monkeypatch.setenv("RETRIEVAL_MIN_SCORE", "0.82")
    retriever = build_retriever(HashEmbedder(), MARKETS)
    assert retriever is not None and retriever.min_score == 0.82


@pytest.mark.parametrize(
    ("header", "attributes"),
    [
        ("check=1834", {"client.check_ms": 1834}),
        (" check=0 ", {"client.check_ms": 0}),
        ("check=120000", {"client.check_ms": 120_000}),
        ("check=120001", {}),
        ("check=12,check=99", {"client.check_ms": 12}),  # only as many parts as keys are read
        ("", {}),
        (None, {}),
    ],
)
def test_client_timing_keeps_only_known_whole_milliseconds(
    header: str | None, attributes: dict[str, int]
) -> None:
    assert client_timing(header) == attributes


def test_the_page_may_send_its_timing_header_cross_origin() -> None:
    page = "https://chat.example"
    client = TestClient(create_app(llm=MockLLM(), origins=[page]))
    preflight = client.options(
        "/v1/chat",
        headers={
            "Origin": page,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,traceparent,x-client-timing",
        },
    )
    assert preflight.status_code == 200
    assert "x-client-timing" in preflight.headers["access-control-allow-headers"].lower()
