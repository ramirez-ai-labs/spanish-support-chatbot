"""Thin HTTP entry point: wires adapters into the core and streams replies as server-sent events.

Run locally with `docker compose up` or `uv run uvicorn --factory apps.api.main:create_app`.
The factory keeps imports side-effect free, so tests never need a Gemini key.
"""

import json
import os
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.chat import MAX_MESSAGE_CHARS, ChatService
from core.ports import LLMProvider


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


def create_app(llm: LLMProvider | None = None) -> FastAPI:
    chat = ChatService(llm or build_llm())
    app = FastAPI(title="spanish-support-chatbot")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat")
    def post_chat(request: ChatRequest) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            try:
                async for delta in chat.reply(request.message):
                    yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
            except Exception:
                # Never echo provider errors or message text to the client or logs.
                yield 'event: error\ndata: {"error": "model_unavailable"}\n\n'
                return
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
