"""Thin HTTP entry point: wires adapters into the core and streams replies as server-sent events.

Run locally with `docker compose up` or `uv run uvicorn --factory apps.api.main:create_app`.
The factory keeps imports side-effect free, so tests never need a Gemini key.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel, Field

from core.chat import MAX_MESSAGE_CHARS, ChatService
from core.identity import ANONYMOUS, Caller, InvalidToken, caller_for
from core.ports import IdentityProvider, LLMProvider

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


def build_llm() -> LLMProvider:
    provider = os.environ.get("LLM_PROVIDER", "gemini")
    if provider == "mock":
        from adapters.shared.mock_llm import MockLLM

        return MockLLM()
    if provider == "gemini":
        from adapters.shared.gemini import DEFAULT_MODEL, GeminiLLM

        return GeminiLLM(
            api_key=os.environ.get("GEMINI_API_KEY", ""),
            model=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL),
        )
    raise ValueError(f"Unknown LLM_PROVIDER: {provider!r} (expected 'gemini' or 'mock')")


def build_identity() -> IdentityProvider | None:
    """Pick the token verifier from IDENTITY_PROVIDER. With none, every token is rejected."""
    provider = os.environ.get("IDENTITY_PROVIDER", "none")
    if provider == "none":
        return None
    if provider == "firebase":
        from adapters.gcp.firebase_auth import firebase_identity_provider

        return firebase_identity_provider(os.environ["GOOGLE_CLOUD_PROJECT"])
    raise ValueError(f"Unknown IDENTITY_PROVIDER: {provider!r} (expected 'firebase' or 'none')")


def build_tracing() -> Callable[[], None] | None:
    """Pick the span exporter from TRACE_EXPORTER; tracing stays a no-op when it is unset."""
    exporter = os.environ.get("TRACE_EXPORTER", "none")
    if exporter == "none":
        return None

    from adapters.shared.tracing import configure_tracing

    sample_ratio = float(os.environ.get("TRACE_SAMPLE_RATIO", "1.0"))
    if exporter == "gcp":
        from adapters.gcp.observability import cloud_trace_exporter, configure_cloud_logging

        project_id = os.environ["GOOGLE_CLOUD_PROJECT"]
        configure_cloud_logging(project_id)
        return configure_tracing(cloud_trace_exporter(project_id), sample_ratio)
    if exporter == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return configure_tracing(ConsoleSpanExporter(), sample_ratio)
    raise ValueError(f"Unknown TRACE_EXPORTER: {exporter!r} (expected 'gcp', 'console' or 'none')")


def create_app(
    llm: LLMProvider | None = None,
    flush_traces: Callable[[], None] | None = None,
    identity: IdentityProvider | None = None,
) -> FastAPI:
    if llm is None:
        llm = build_llm()
        flush_traces = build_tracing()
        identity = build_identity()
    chat = ChatService(llm)

    def identify(authorization: str | None) -> Caller | None:
        """Anonymous without a token; None (reject with 401) for a token that does not verify."""
        if authorization is None:
            return ANONYMOUS
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        try:
            if scheme.lower() != "bearer" or not token:
                raise InvalidToken("malformed_header")
            if identity is None:
                raise InvalidToken("no_identity_provider")
            return caller_for(identity.verify(token), now=time.time())
        except InvalidToken as exc:
            # The reason code only: never the token or its claims.
            logger.info("auth_rejected reason=%s", exc.reason)
            return None

    # FastAPI's built-in telemetry is off. Its request span ends after the response is sent,
    # when serverless CPU is already throttled, so it would miss the flush below, and its logs
    # option records validation errors, which can include the message text.
    app = FastAPI(
        title="spanish-support-chatbot",
        telemetry={"tracing": False, "metrics": False, "logs": False},
    )

    # Not /healthz: Cloud Run reserves some paths ending in "z" and may never route them here.
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat")
    def post_chat(request: ChatRequest, http: Request) -> Response:
        # Every call is checked: the tier comes from this request's token, not from earlier ones.
        # The token stops here; only the message text goes on to the chat service and the model.
        caller = identify(http.headers.get("authorization"))
        if caller is None:
            return JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        # Continues the browser's trace when it sends `traceparent`; otherwise starts one.
        parent = propagate.extract(http.headers)

        async def events() -> AsyncIterator[str]:
            # The request span covers the whole stream, so it is started here, not by middleware.
            span = tracer.start_span(
                "POST /v1/chat",
                context=parent,
                kind=SpanKind.SERVER,
                attributes={
                    "http.request.method": "POST",
                    "http.route": "/v1/chat",
                    "chat.outcome": "incomplete",  # stays so if the client leaves mid-stream
                    "auth.tier": caller.tier.value,
                },
            )
            try:
                with trace.use_span(span, record_exception=False, set_status_on_exception=False):
                    async for event in stream_chat(span):
                        yield event
            finally:
                span.end()
                if flush_traces is not None:
                    await asyncio.to_thread(flush_traces)

        async def stream_chat(span: trace.Span) -> AsyncIterator[str]:
            try:
                async for delta in chat.reply(request.message):
                    yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
            except Exception as exc:
                # Never echo provider errors or message text to the client or logs: provider
                # messages can quote the request. Log only the error type and HTTP status code.
                code = getattr(exc, "code", None)
                logger.warning(
                    "llm_error type=%s code=%s",
                    type(exc).__name__,
                    code if isinstance(code, int) else "none",
                )
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", type(exc).__name__)
                span.set_attribute("chat.outcome", "error")
                yield 'event: error\ndata: {"error": "model_unavailable"}\n\n'
                return
            span.set_attribute("chat.outcome", "done")
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
