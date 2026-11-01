import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from adapters.shared.memory_usage import MemoryUsageSink
from adapters.shared.mock_llm import MOCK_REPLY, MockLLM
from adapters.shared.neon_usage import NeonUsageSink
from apps.api.main import build_usage_sink, create_app
from core.chat import PROMPT_PARTS, ChatService
from core.usage import LLMUsage, TokenUsage

MESSAGE = "mi tarjeta termina en 4417, ¿cómo la bloqueo?"


def run_reply(chat: ChatService, stop_after: int | None = None) -> list[str]:
    async def consume() -> list[str]:
        out: list[str] = []
        stream = chat.reply(MESSAGE)
        async for delta in stream:
            out.append(delta)
            if stop_after is not None and len(out) == stop_after:
                await stream.aclose()  # type: ignore[attr-defined]  # a client leaving
                break
        return out

    return asyncio.run(consume())


def test_a_finished_answer_writes_one_record() -> None:
    sink = MemoryUsageSink()
    run_reply(ChatService(MockLLM(), usage=sink, cloud="gcp"))

    (record,) = sink.records
    assert (record.cloud, record.model, record.purpose) == ("gcp", "mock", "chat")
    assert record.prompt_parts == PROMPT_PARTS
    assert record.outcome == "done"
    assert record.tokens.input_tokens > 0
    assert record.tokens.output_tokens == len(MOCK_REPLY.split(" "))
    assert record.ttft_ms is not None and record.latency_ms >= record.ttft_ms


def test_the_record_never_holds_message_text() -> None:
    sink = MemoryUsageSink()
    run_reply(ChatService(MockLLM(), usage=sink))

    assert "4417" not in repr(sink.records[0])
    assert MESSAGE not in repr(sink.records[0])


def test_a_client_leaving_mid_answer_is_recorded_as_incomplete() -> None:
    sink = MemoryUsageSink()
    run_reply(ChatService(MockLLM(), usage=sink), stop_after=2)

    (record,) = sink.records
    assert record.outcome == "incomplete"


def test_a_provider_error_is_recorded_and_still_raised() -> None:
    class FailingLLM:
        model = "fake"

        async def stream(
            self, system: str, user_message: str, usage: TokenUsage | None = None
        ) -> AsyncIterator[str]:
            raise RuntimeError("provider detail")
            yield ""

    sink = MemoryUsageSink()
    with pytest.raises(RuntimeError):
        run_reply(ChatService(FailingLLM(), usage=sink))

    (record,) = sink.records
    assert record.outcome == "error"
    assert record.ttft_ms is None


def test_each_chat_request_writes_one_record() -> None:
    sink = MemoryUsageSink()
    client = TestClient(create_app(llm=MockLLM(), usage=sink))

    client.post("/v1/chat", json={"message": "hola"})
    client.post("/v1/chat", json={"message": "adiós"})

    assert [r.outcome for r in sink.records] == ["done", "done"]


def test_a_broken_database_never_fails_the_chat(caplog: pytest.LogCaptureFixture) -> None:
    # Port 1 refuses connections immediately: the write fails, and only its type is logged.
    sink = NeonUsageSink("postgresql://usage_writer:secret@127.0.0.1:1/chatbot")
    record = LLMUsage(
        "local", "mock", "chat", PROMPT_PARTS, TokenUsage(1, 0, 1, 0), 5, 2, "done", None
    )

    with caplog.at_level("WARNING"):
        asyncio.run(sink.record(record))  # must not raise

    assert "usage_write_failed type=" in caplog.text
    assert "secret" not in caplog.text


def test_usage_sink_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USAGE_SINK", raising=False)
    assert build_usage_sink() is None
    monkeypatch.setenv("USAGE_SINK", "neon")
    monkeypatch.setenv("NEON_USAGE_URL", "postgresql://u:p@127.0.0.1:1/db")
    assert isinstance(build_usage_sink(), NeonUsageSink)
    monkeypatch.setenv("USAGE_SINK", "nope")
    with pytest.raises(ValueError, match="Unknown USAGE_SINK"):
        build_usage_sink()
