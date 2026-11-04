"""Thin HTTP entry point: wires adapters into the core and streams replies as server-sent events.

Run locally with `docker compose up` or `uv run uvicorn --factory apps.api.main:create_app`.
The factory keeps imports side-effect free, so tests never need a Gemini key.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel, Field

from core.bot_check import BotCheckFailed
from core.chat import MAX_MESSAGE_CHARS, ChatService, KnowledgeUnavailable, Sources
from core.identity import ANONYMOUS, Caller, IdentityUnavailable, InvalidToken, caller_for
from core.knowledge import Market, load_docs, load_markets
from core.ports import (
    BotCheck,
    DeployProbe,
    Embedder,
    IdentityProvider,
    LLMProvider,
    PIIDetector,
    SignInLinkSender,
    UsageSink,
)
from core.retrieval import Retriever
from core.safety import ContentBlocked
from core.sign_in import MAX_EMAIL_CHARS, SignInLinkFailed, looks_like_email

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# knowledge/ in the repo, and /app/knowledge in the image: the market list and the docs.
KNOWLEDGE = Path(os.environ.get("KNOWLEDGE_DIR", Path(__file__).resolve().parents[2] / "knowledge"))


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    # A code from GET /v1/markets; required, since every answer comes from one market's policy.
    # Optional in the schema only so that a missing market is refused after the token and bot
    # checks (400 market_required), never ahead of them with a 422.
    market: str | None = Field(default=None, max_length=8)


class SignInLinkRequest(BaseModel):
    email: str = Field(min_length=3, max_length=MAX_EMAIL_CHARS)


# Browser-side timings the page sends with each message (X-Client-Timing: check=1234), so a trace
# covers the wait before the request left the page, which the server can't see. Untrusted: only
# these keys, whole milliseconds up to two minutes; anything else is ignored, never refused.
CLIENT_TIMINGS = {"check": "client.check_ms"}  # check: the wait for the bot-check token
MAX_CLIENT_TIMING_MS = 120_000


def client_timing(header: str | None) -> dict[str, int]:
    """Span attributes from an X-Client-Timing header; empty when it's missing or malformed."""
    attributes: dict[str, int] = {}
    for part in (header or "").split(",")[: len(CLIENT_TIMINGS)]:
        key, _, value = part.strip().partition("=")
        if key in CLIENT_TIMINGS and value.isdigit() and int(value) <= MAX_CLIENT_TIMING_MS:
            attributes[CLIENT_TIMINGS[key]] = int(value)
    return attributes


# Status codes for the refusals the API returns before doing any work.
SIGN_IN_STATUS = {"invalid_email": 400, "too_many_attempts": 429, "sign_in_unavailable": 503}


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


def build_sign_in() -> SignInLinkSender | None:
    """Pick who sends sign-in links from SIGN_IN_SENDER; none by default (tests, local runs)."""
    sender = os.environ.get("SIGN_IN_SENDER", "none")
    if sender == "none":
        return None
    if sender == "identity_platform":
        from adapters.gcp.identity_platform_sign_in import IdentityPlatformSignInLinkSender

        return IdentityPlatformSignInLinkSender(
            os.environ["GOOGLE_CLOUD_PROJECT"], os.environ["SIGN_IN_CONTINUE_URL"]
        )
    raise ValueError(f"Unknown SIGN_IN_SENDER: {sender!r} (expected 'identity_platform' or 'none')")


def build_embedder() -> Embedder | None:
    """Pick the embedder from EMBEDDER; none by default (tests, local runs without the model).

    Loaded once at startup, so its cost lands on the cold start, not on the first question.
    """
    choice = os.environ.get("EMBEDDER", "none")
    if choice == "none":
        return None
    if choice == "hash":
        from adapters.shared.hash_embedder import HashEmbedder

        return HashEmbedder()
    if choice == "e5":
        from pathlib import Path

        from adapters.shared.e5 import E5Embedder

        started = time.perf_counter()
        embedder = E5Embedder(Path(os.environ["E5_MODEL_DIR"]))
        embedder.embed_query("hola")  # first run allocates ONNX Runtime's buffers
        logger.info(
            "embedder_loaded model=%s ms=%d", embedder.model, (time.perf_counter() - started) * 1000
        )
        return embedder
    raise ValueError(f"Unknown EMBEDDER: {choice!r} (expected 'e5', 'hash' or 'none')")


def build_pii_detector() -> PIIDetector | None:
    """Pick the PII detector from PII_DETECTOR; none by default (tests, local runs).

    Loaded once at startup, like the embedder. Not used on the chat path yet: redaction is wired
    in next (ADR 0008), and this first lands the model so its size, memory and cold start are
    measured on the live service (#51).
    """
    choice = os.environ.get("PII_DETECTOR", "none")
    if choice == "none":
        return None
    if choice == "presidio":
        from adapters.shared.presidio import SPACY_MODEL, PresidioDetector

        started = time.perf_counter()
        detector = PresidioDetector()
        logger.info(
            "pii_detector_loaded model=%s ms=%d",
            SPACY_MODEL,
            (time.perf_counter() - started) * 1000,
        )
        return detector
    raise ValueError(f"Unknown PII_DETECTOR: {choice!r} (expected 'presidio' or 'none')")


def build_retriever(embedder: Embedder | None, markets: Mapping[str, Market]) -> Retriever | None:
    """Pick where retrieval searches from KNOWLEDGE_STORE; none by default (tests, local runs).

    - neon: the `chunks` table as the read-only app role (NEON_KNOWLEDGE_URL), filled by the
      indexing workflow.
    - memory: indexes knowledge/ into memory at startup, for local runs and the image's CI test.
    RETRIEVAL_MIN_SCORE drops chunks below it; it's calibrated per embedding model (#86).
    """
    store = os.environ.get("KNOWLEDGE_STORE", "none")
    if store == "none":
        return None
    if embedder is None:
        raise ValueError(f"KNOWLEDGE_STORE={store} needs an EMBEDDER")
    min_score = float(os.environ.get("RETRIEVAL_MIN_SCORE", "0"))
    if store == "neon":
        from adapters.shared.neon_knowledge import NeonKnowledgeStore

        knowledge = NeonKnowledgeStore(os.environ["NEON_KNOWLEDGE_URL"])
        return Retriever(embedder, knowledge, markets, min_score=min_score)
    if store == "memory":
        from adapters.shared.memory_chunks import MemoryChunkStore
        from adapters.shared.memory_knowledge import MemoryKnowledgeStore
        from core.indexing import index

        chunks = MemoryChunkStore()
        index(load_docs(KNOWLEDGE / "docs"), embedder, chunks)
        return Retriever(embedder, MemoryKnowledgeStore(chunks.rows.values()), markets, min_score)
    raise ValueError(f"Unknown KNOWLEDGE_STORE: {store!r} (expected 'neon', 'memory' or 'none')")


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
    sign_in: SignInLinkSender | None = None,
    retriever: Retriever | None = None,
    markets: Mapping[str, Market] | None = None,
    pii_detector: PIIDetector | None = None,
) -> FastAPI:
    if markets is None:
        markets = load_markets(KNOWLEDGE / "markets.yaml")
    if llm is None:
        llm = build_llm()
        flush_traces = build_tracing()
        identity = build_identity()
        usage = build_usage_sink()
        bot_check = build_bot_check()
        deploy_probe = build_deploy_probe()
        origins = allowed_origins()
        sign_in = build_sign_in()
        retriever = build_retriever(build_embedder(), markets)
        pii_detector = build_pii_detector()
    chat = ChatService(
        llm, usage=usage, cloud=os.environ.get("CLOUD", "local"), retriever=retriever
    )

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

    def check_bot(token: str | None) -> tuple[str, int] | None:
        """The bot check's refusal as (reason, status), or None when it passes."""
        if bot_check is None:
            return None
        try:
            bot_check.verify(token)
        except BotCheckFailed as exc:
            return exc.reason, 503 if exc.reason == "turnstile_unavailable" else 403
        return None

    def end_rejected(span: trace.Span, reason: str, status: int) -> None:
        span.set_attributes(
            {
                "chat.outcome": "rejected",
                "auth.rejected_reason": reason,
                "http.response.status_code": status,
            }
        )
        span.end()  # a 4xx is the client's error, so the span status stays unset
        if flush_traces is not None:
            flush_traces()  # handlers run in a worker thread, so blocking is fine

    # FastAPI's built-in telemetry is off. Its request span ends after the response is sent,
    # when serverless CPU is already throttled, so it would miss the flush below, and its logs
    # option records validation errors, which can include the message text.
    app = FastAPI(
        title="spanish-support-chatbot",
        telemetry={"tracing": False, "metrics": False, "logs": False},
    )
    # Held, not yet used: kept referenced so the model stays loaded and the live service's memory
    # is measured with it (#51). Redaction, the next slice, passes it to the chat service.
    app.state.pii_detector = pii_detector
    # Only the chat page's origin may call the API from a browser. The headers are the ones the
    # page sends: its user's token, its trace context and its bot-check token.
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST"],
            allow_headers=[
                "content-type",
                "authorization",
                "traceparent",
                "cf-turnstile-response",
                "x-client-timing",
            ],
            max_age=600,
        )

    # Not /healthz: Cloud Run reserves some paths ending in "z" and may never route them here.
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # The page's market selector (#89): the codes chat accepts and their names, from config.
    @app.get("/v1/markets")
    def get_markets() -> dict[str, list[dict[str, str]]]:
        return {"markets": [{"code": m.code, "name": m.name} for m in markets.values()]}

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
                **client_timing(http.headers.get("x-client-timing")),
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
                if not probe:
                    rejection = check_bot(http.headers.get("cf-turnstile-response"))
            except InvalidToken as exc:
                rejection = (exc.reason, 401)
            except IdentityUnavailable:
                rejection = ("auth_unavailable", 503)  # fail closed, but keep the page's session
            if rejection is None and request.market is None:
                rejection = ("market_required", 400)  # the page's own word for it
            elif rejection is None and request.market not in markets:
                rejection = ("unknown_market", 400)
            if rejection is not None:
                # The reason code only: never a token or its claims. Logged inside the span,
                # so Cloud Logging links the line to this request's trace.
                logger.info("request_rejected reason=%s", rejection[0])
        if rejection is not None:
            reason, status = rejection
            end_rejected(span, reason, status)
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
                "chat.market": request.market or "none",
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
                async for event in chat.reply(request.message, request.market):
                    if isinstance(event, Sources):
                        # Before the first delta, so the page can show them as the answer streams.
                        sources = [vars(source) for source in event.sources]
                        data = json.dumps({"sources": sources}, ensure_ascii=False)
                        yield f"event: sources\ndata: {data}\n\n"
                    else:
                        yield f"data: {json.dumps({'delta': event}, ensure_ascii=False)}\n\n"
            except KnowledgeUnavailable as exc:
                cause = type(exc.__cause__).__name__ if exc.__cause__ else "none"
                logger.warning("knowledge_error type=%s", cause)
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", cause)
                span.set_attribute("chat.outcome", "error")
                yield 'event: error\ndata: {"error": "knowledge_unavailable"}\n\n'
                return
            except ContentBlocked as exc:
                # The page replaces anything already shown with a polite refusal. Categories only,
                # never the text.
                logger.info(
                    "content_blocked stage=%s categories=%s", exc.stage, ",".join(exc.categories)
                )
                span.set_attribute("chat.outcome", "blocked")
                yield 'event: error\ndata: {"error": "content_blocked"}\n\n'
                return
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

    @app.post("/v1/sign-in-link")
    def post_sign_in_link(request: SignInLinkRequest, http: Request) -> Response:
        # Behind the same bot check as chat, so the page can't be used to email strangers in bulk.
        # The address goes only to the sender: never on the span, in a log line or in a reply.
        span = tracer.start_span(
            "POST /v1/sign-in-link",
            context=propagate.extract(http.headers),
            kind=SpanKind.SERVER,
            attributes={"http.request.method": "POST", "http.route": "/v1/sign-in-link"},
        )
        with trace.use_span(span, record_exception=False, set_status_on_exception=False):
            rejection = check_bot(http.headers.get("cf-turnstile-response"))
            if rejection is None and not looks_like_email(request.email):
                rejection = ("invalid_email", 400)
            if rejection is None:
                try:
                    if sign_in is None:
                        raise SignInLinkFailed("sign_in_unavailable")
                    sign_in.send(request.email)
                except SignInLinkFailed as exc:
                    rejection = (exc.reason, SIGN_IN_STATUS.get(exc.reason, 503))
            if rejection is not None:
                logger.info("request_rejected reason=%s", rejection[0])
            else:
                logger.info("sign_in_link_sent")
        if rejection is not None:
            reason, status = rejection
            end_rejected(span, reason, status)
            return JSONResponse({"error": reason}, status_code=status)
        span.set_attributes({"chat.outcome": "sent", "http.response.status_code": 202})
        span.end()
        if flush_traces is not None:
            flush_traces()
        return JSONResponse({"status": "sent"}, status_code=202)

    return app
