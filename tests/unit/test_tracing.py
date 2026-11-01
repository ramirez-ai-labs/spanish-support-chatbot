import json
import logging
import sys
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from adapters.gcp.observability import CloudLoggingFormatter
from adapters.shared.mock_llm import MockLLM
from adapters.shared.tracing import configure_tracing
from apps.api.main import LLMProvider, build_tracing, create_app

# The global tracer provider can be set once per process, so this module owns it.
EXPORTER = InMemorySpanExporter()
FLUSH = configure_tracing(EXPORTER, sample_ratio=1.0)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_SPAN_ID = "00f067aa0ba902b7"


@pytest.fixture(autouse=True)
def clear_spans() -> Iterator[None]:
    EXPORTER.clear()
    yield
    EXPORTER.clear()


def chat(llm: LLMProvider, message: str = "hola", headers: dict[str, str] | None = None) -> None:
    client = TestClient(create_app(llm=llm, flush_traces=FLUSH))
    client.post("/v1/chat", json={"message": message}, headers=headers or {})


def spans_by_name() -> dict[str, ReadableSpan]:
    return {span.name: span for span in EXPORTER.get_finished_spans()}


def test_request_is_one_trace_that_continues_the_browser_traceparent() -> None:
    chat(MockLLM(), headers={"traceparent": f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01"})

    spans = spans_by_name()
    assert set(spans) == {"POST /v1/chat", "chat.model"}
    request, model = spans["POST /v1/chat"], spans["chat.model"]
    assert request.kind == SpanKind.SERVER
    assert request.parent is not None and f"{request.parent.span_id:016x}" == PARENT_SPAN_ID
    assert {f"{s.context.trace_id:032x}" for s in spans.values()} == {TRACE_ID}
    assert model.parent is not None and model.parent.span_id == request.context.span_id
    assert request.attributes is not None and request.attributes["chat.outcome"] == "done"
    assert model.attributes is not None
    assert model.attributes["gen_ai.request.model"] == "mock"
    assert isinstance(model.attributes["llm.time_to_first_token_ms"], int)


def test_not_sampled_parent_from_the_front_end_is_still_traced() -> None:
    chat(MockLLM(), headers={"traceparent": f"00-{TRACE_ID}-{PARENT_SPAN_ID}-00"})

    assert {f"{s.context.trace_id:032x}" for s in EXPORTER.get_finished_spans()} == {TRACE_ID}


def test_spans_never_carry_message_text() -> None:
    message = "mi tarjeta termina en 4417 y mi nombre es Ana"
    chat(MockLLM(), message=message)

    for span in EXPORTER.get_finished_spans():
        values = [str(v) for v in (span.attributes or {}).values()]
        values += [str(v) for event in span.events for v in (event.attributes or {}).values()]
        assert not any("4417" in v or "Ana" in v for v in values), span.name
    model_attributes = spans_by_name()["chat.model"].attributes
    assert model_attributes is not None and model_attributes["chat.message_chars"] == len(message)


def test_provider_error_marks_both_spans_by_type_only() -> None:
    secret = "provider detail quoting: hola"

    class FailingLLM:
        model = "fake"

        async def stream(self, system: str, user_message: str) -> AsyncIterator[str]:
            raise RuntimeError(secret)
            yield ""

    chat(FailingLLM())

    spans = spans_by_name()
    for span in spans.values():
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes is not None and span.attributes["error.type"] == "RuntimeError"
        assert span.events == ()  # no exception event, which would carry the message
    assert spans["POST /v1/chat"].attributes is not None
    assert spans["POST /v1/chat"].attributes["chat.outcome"] == "error"


def test_cloud_logging_lines_link_to_the_current_trace() -> None:
    formatter = CloudLoggingFormatter("demo-project")
    record = logging.LogRecord("apps.api.main", logging.WARNING, __file__, 1, "llm_error", (), None)

    with trace.get_tracer(__name__).start_as_current_span("request") as span:
        entry = json.loads(formatter.format(record))
        context = span.get_span_context()

    assert entry["severity"] == "WARNING"
    assert entry["message"] == "llm_error"
    assert entry["logging.googleapis.com/trace"] == (
        f"projects/demo-project/traces/{context.trace_id:032x}"
    )
    assert entry["logging.googleapis.com/spanId"] == f"{context.span_id:016x}"


def test_cloud_logging_logs_exceptions_by_type_only() -> None:
    try:
        raise ValueError("quotes the user: hola")
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())

    line = CloudLoggingFormatter("demo-project").format(record)

    assert json.loads(line)["error.type"] == "ValueError"
    assert "quotes the user" not in line


def test_tracing_is_off_unless_an_exporter_is_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACE_EXPORTER", raising=False)
    assert build_tracing() is None


def test_unknown_trace_exporter_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACE_EXPORTER", "nope")
    with pytest.raises(ValueError, match="Unknown TRACE_EXPORTER"):
        build_tracing()
