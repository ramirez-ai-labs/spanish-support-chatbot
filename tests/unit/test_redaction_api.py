"""Redaction in the chat API (ADR 0008): before retrieval and the model, and failing closed."""

import logging
from collections.abc import AsyncIterator, Sequence

import pytest
from fastapi.testclient import TestClient

from adapters.shared.hash_embedder import HashEmbedder
from adapters.shared.mock_llm import MockLLM
from apps.api.main import KNOWLEDGE, build_retriever, create_app
from core.knowledge import load_markets
from core.pii import DetectorUnavailable, PIISpan

MARKETS = load_markets(KNOWLEDGE / "markets.yaml")
EMAIL = "ana@example.com"
MESSAGE = f"Soy Ana, mi correo es {EMAIL}. ¿Cómo bloqueo mi tarjeta?"


class EmailDetector:
    """Finds the one synthetic address: enough to see where redaction runs."""

    def detect(self, text: str) -> list[PIISpan]:
        start = text.find(EMAIL)
        return [] if start == -1 else [PIISpan("EMAIL_ADDRESS", start, start + len(EMAIL), 1.0)]


class BrokenDetector:
    def detect(self, text: str) -> list[PIISpan]:
        raise DetectorUnavailable("RuntimeError")


class RecordingLLM:
    model = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def stream(
        self, system: str, user_message: str, usage: object = None
    ) -> AsyncIterator[str]:
        self.prompts.append(user_message)
        yield "ok"


class RecordingEmbedder(HashEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.queries: list[str] = []

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return super().embed_query(text)

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return super().embed_passages(texts)


def test_the_model_and_the_query_vector_get_the_redacted_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNOWLEDGE_STORE", "memory")
    llm, embedder = RecordingLLM(), RecordingEmbedder()
    app = create_app(
        llm=llm, retriever=build_retriever(embedder, MARKETS), pii_detector=EmailDetector()
    )

    response = TestClient(app).post("/v1/chat", json={"message": MESSAGE, "market": "MX"})

    assert response.status_code == 200
    assert embedder.queries and all(EMAIL not in q for q in embedder.queries)
    assert "<EMAIL_ADDRESS_1>" in embedder.queries[-1]
    assert len(llm.prompts) == 1
    assert EMAIL not in llm.prompts[0]
    assert "<EMAIL_ADDRESS_1>" in llm.prompts[0]


def test_redaction_is_logged_by_type_never_by_value(caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(create_app(llm=MockLLM(), pii_detector=EmailDetector()))
    with caplog.at_level(logging.INFO, logger="apps.api.main"):
        client.post("/v1/chat", json={"message": MESSAGE, "market": "US"})

    assert "pii_redacted stage=input types=EMAIL_ADDRESS:1" in caplog.text
    assert EMAIL not in caplog.text


def test_a_message_without_pii_logs_no_redaction(caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(create_app(llm=MockLLM(), pii_detector=EmailDetector()))
    with caplog.at_level(logging.INFO, logger="apps.api.main"):
        client.post("/v1/chat", json={"message": "hola", "market": "US"})

    assert "pii_redacted" not in caplog.text


def test_a_detector_failure_refuses_the_message_before_the_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm = RecordingLLM()
    client = TestClient(create_app(llm=llm, pii_detector=BrokenDetector()))
    with caplog.at_level(logging.INFO, logger="apps.api.main"):
        response = client.post("/v1/chat", json={"message": MESSAGE, "market": "US"})

    assert response.status_code == 503
    assert response.json() == {"error": "guardrails_unavailable"}
    assert llm.prompts == []
    assert "guardrails_error type=DetectorUnavailable" in caplog.text
    assert EMAIL not in caplog.text


def test_a_cloud_deploy_without_a_detector_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CLOUD", "gcp")
    monkeypatch.delenv("PII_DETECTOR", raising=False)
    with pytest.raises(RuntimeError, match="PII_DETECTOR is required"):
        create_app()


def test_local_runs_may_go_without_a_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("CLOUD", raising=False)
    monkeypatch.delenv("PII_DETECTOR", raising=False)
    response = TestClient(create_app()).post("/v1/chat", json={"message": "hola", "market": "US"})
    assert response.status_code == 200
