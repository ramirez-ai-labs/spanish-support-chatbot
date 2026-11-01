"""Gemini adapter for the LLMProvider port (google-genai SDK, free-tier AI Studio key)."""

from collections.abc import AsyncIterator

from google import genai
from google.genai import types

from core.usage import TokenUsage

# Free-tier Flash-Lite: ~0.5 s to first token with thinking at "minimal", where Flash 3.8 took
# over a minute on the free tier. gemini-2.5-* is closed to new keys. The M4 bakeoff picks the
# final model; override with GEMINI_MODEL meanwhile.
DEFAULT_MODEL = "gemini-3.5-flash-lite"


class GeminiLLM:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is empty; set it in .env (see .env.example)")
        self._client = genai.Client(api_key=api_key)
        self.model = model

    async def stream(
        self, system: str, user_message: str, usage: TokenUsage | None = None
    ) -> AsyncIterator[str]:
        config = types.GenerateContentConfig(
            system_instruction=system,
            # Thinking as low as the model allows on the hot path: it adds latency and tokens to
            # time to first token. Gemini 3 ignores thinking_budget=0, so use the level.
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
        )
        chunks = await self._client.aio.models.generate_content_stream(
            model=self.model, contents=user_message, config=config
        )
        async for chunk in chunks:
            # Counts arrive on the stream's chunks; the last one has the totals for the call.
            # prompt_token_count includes cached tokens; candidates excludes thinking.
            meta = chunk.usage_metadata
            if usage is not None and meta is not None:
                usage.input_tokens = meta.prompt_token_count or usage.input_tokens
                usage.cached_tokens = meta.cached_content_token_count or usage.cached_tokens
                usage.output_tokens = meta.candidates_token_count or usage.output_tokens
                usage.thinking_tokens = meta.thoughts_token_count or usage.thinking_tokens
            if chunk.text:
                yield chunk.text
