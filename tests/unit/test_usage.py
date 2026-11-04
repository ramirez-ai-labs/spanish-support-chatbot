import asyncio
from collections.abc import AsyncIterator

import psycopg
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

    client.post("/v1/chat", json={"message": "hola", "market": "US"})
    client.post("/v1/chat", json={"message": "adiós", "market": "US"})

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


class FakeConnection:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.closed = False
        self.inserts = 0

    async def execute(self, query: str, params: object) -> None:
        if self.fail:
            self.closed = True
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        self.inserts += 1

    async def close(self) -> None:
        self.closed = True


def test_the_first_write_after_neon_scaled_to_zero_reconnects_and_keeps_the_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The reused connection is dead (Neon closed it while idle); a fresh one works.
    dead, fresh = FakeConnection(fail=True), FakeConnection(fail=False)
    opened = iter([dead, fresh])

    async def connect() -> FakeConnection:
        return next(opened)

    sink = NeonUsageSink("unused", connect=connect)
    record = LLMUsage(
        "gcp", "mock", "chat", PROMPT_PARTS, TokenUsage(1, 0, 1, 0), 5, 2, "done", None
    )

    with caplog.at_level("INFO"):
        asyncio.run(sink.record(record))

    assert fresh.inserts == 1  # the row was written on the retry
    assert "usage_write_failed" not in caplog.text
    assert "usage_write_reconnect" in caplog.text


class SlowConnection(FakeConnection):
    def __init__(self, fail: bool, delay: float) -> None:
        super().__init__(fail)
        self.delay = delay

    async def execute(self, query: str, params: object) -> None:
        await asyncio.sleep(self.delay)
        await super().execute(query, params)


def test_a_slow_reconnect_still_fits_the_write_deadline(caplog: pytest.LogCaptureFixture) -> None:
    # Each attempt fits its own timeout, but together they take longer than one: the deadline for
    # the whole write must cover the retry, or this exact case (Neon waking up) loses the row.
    dead, fresh = SlowConnection(fail=True, delay=0.15), SlowConnection(fail=False, delay=0.15)
    opened = iter([dead, fresh])

    async def connect() -> FakeConnection:
        return next(opened)

    sink = NeonUsageSink("unused", connect=connect, timeout_seconds=0.2)
    record = LLMUsage(
        "gcp", "mock", "chat", PROMPT_PARTS, TokenUsage(1, 0, 1, 0), 5, 2, "done", None
    )

    with caplog.at_level("INFO"):
        asyncio.run(sink.record(record))

    assert fresh.inserts == 1
    assert "usage_write_failed" not in caplog.text
