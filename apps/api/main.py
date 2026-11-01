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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel, Field

from core.bot_check import BotCheckFailed
from core.chat import MAX_MESSAGE_CHARS, ChatService
from core.identity import ANONYMOUS, Caller, InvalidToken, caller_for
from core.ports import BotCheck, DeployProbe, IdentityProvider, LLMProvider, UsageSink

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


def allowed_origins() -> list[str]:
    """Browser origins allowed to call the API (the chat page), from ALLOWED_ORIGINS."""
    return [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]


def build_bot_check() -> BotCheck | None:
    """Pick the bot check from BOT_CHECK; none by default (tests, local runs)."""
    check = os.environ.get("BOT_CHECK", "none")
    if check == "none":
        return None
    if check == "turnstile":
        from urllib.parse import urlsplit

        from adapters.shared.turnstile import TurnstileBotCheck

        hostnames = {urlsplit(origin).hostname or "" for origin in allowed_origins()}
        return TurnstileBotCheck(os.environ["TURNSTILE_SECRET"], hostnames - {""})
    raise ValueError(f"Unknown BOT_CHECK: {check!r} (expected 'turnstile' or 'none')")


def build_deploy_probe() -> DeployProbe | None:
    """The deploy job's signed identity, from DEPLOY_PROBE_EMAIL; none when it isn't set."""
    email = os.environ.get("DEPLOY_PROBE_EMAIL")
    if not email:
        return None
    from adapters.gcp.deploy_probe import GoogleDeployProbe

    return GoogleDeployProbe(email)


def build_usage_sink() -> UsageSink | None:
    """Pick where usage records go from USAGE_SINK; none by default (tests, local runs)."""
    sink = os.environ.get("USAGE_SINK", "none")
    if sink == "none":
        return None
    if sink == "neon":
        from adapters.shared.neon_usage import NeonUsageSink

        return NeonUsageSink(os.environ["NEON_USAGE_URL"])
    raise ValueError(f"Unknown USAGE_SINK: {sink!r} (expected 'neon' or 'none')")


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
    usage: UsageSink | None = None,
    bot_check: BotCheck | None = None,
    deploy_probe: DeployProbe | None = None,
    origins: list[str] | None = None,
) -> FastAPI:
    if llm is None:
        llm = build_llm()
        flush_traces = build_tracing()
        identity = build_identity()
        usage = build_usage_sink()
        bot_check = build_bot_check()
        deploy_probe = build_deploy_probe()
        origins = allowed_origins()
    chat = ChatService(llm, usage=usage, cloud=os.environ.get("CLOUD", "local"))

    def identify(authorization: str | None) -> Caller:
        """Anonymous without a token; raises InvalidToken for a token that does not verify."""
        if authorization is None:
            return ANONYMOUS
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise InvalidToken("malformed_header")
        if identity is None:
            raise InvalidToken("no_identity_provider")
        return caller_for(identity.verify(token), now=time.time())

    def is_deploy_probe(header: str | None) -> bool:
        """True for the deploy job's signed identity; raises InvalidToken for a bad one."""
        if header is None:
            return False
        if deploy_probe is None:
            raise InvalidToken("no_deploy_probe")
        deploy_probe.verify(header)
        return True

    # FastAPI's built-in telemetry is off. Its request span ends after the response is sent,
    # when serverless CPU is already throttled, so it would miss the flush below, and its logs
    # option records validation errors, which can include the message text.
    app = FastAPI(
        title="spanish-support-chatbot",
        telemetry={"tracing": False, "metrics": False, "logs": False},
    )

    # Only the chat page's origin may call the API from a browser. The headers are the ones the
    # page sends: its user's token, its trace context and its bot-check token.
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["POST"],
            allow_headers=["content-type", "authorization", "traceparent", "cf-turnstile-response"],
            max_age=600,
        )

    # Not /healthz: Cloud Run reserves some paths ending in "z" and may never route them here.
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat")
    def post_chat(request: ChatRequest, http: Request) -> Response:
        # The request span starts before the token check, so refused requests are traced too. It
        # continues the browser's trace when it sends `traceparent`, and covers the whole stream,
        # which is why it is started here rather than by middleware.
        span = tracer.start_span(
            "POST /v1/chat",
            context=propagate.extract(http.headers),
            kind=SpanKind.SERVER,
            attributes={
                "http.request.method": "POST",
                "http.route": "/v1/chat",
                "chat.outcome": "incomplete",  # stays so if the client leaves mid-stream
            },
        )

        # Every call is checked: the tier comes from this request's token, not from earlier ones,
        # and then the bot check (unless it's the deploy job's own probe). Tokens stop here; only
        # the message text goes on to the chat service and the model.
        caller, probe = ANONYMOUS, False
        rejection: tuple[str, int] | None = None
        with trace.use_span(span, record_exception=False, set_status_on_exception=False):
            try:
                caller = identify(http.headers.get("authorization"))
                probe = is_deploy_probe(http.headers.get("x-deploy-probe"))
                if bot_check is not None and not probe:
                    bot_check.verify(http.headers.get("cf-turnstile-response"))
            except InvalidToken as exc:
                rejection = (exc.reason, 401)
            except BotCheckFailed as exc:
                rejection = (exc.reason, 503 if exc.reason == "turnstile_unavailable" else 403)
            if rejection is not None:
                # The reason code only: never a token or its claims. Logged inside the span,
                # so Cloud Logging links the line to this request's trace.
                logger.info("request_rejected reason=%s", rejection[0])
        if rejection is not None:
            reason, status = rejection
            span.set_attributes(
                {
                    "chat.outcome": "rejected",
                    "auth.rejected_reason": reason,
                    "http.response.status_code": status,
                }
            )
            span.end()  # a 4xx is the client's error, so the span status stays unset
            if flush_traces is not None:
                flush_traces()  # this handler runs in a worker thread, so blocking is fine
            if status == 401:
                return JSONResponse(
                    {"error": "invalid_token"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                )
            return JSONResponse({"error": reason}, status_code=status)
        span.set_attributes(
            {
                "auth.tier": caller.tier.value,
                "auth.deploy_probe": probe,
                "http.response.status_code": 200,
            }
        )

        async def events() -> AsyncIterator[str]:
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
