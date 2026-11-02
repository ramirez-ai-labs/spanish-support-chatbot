"""Gemini adapter for the LLMProvider port (google-genai SDK, free-tier AI Studio key)."""

from collections.abc import AsyncIterator

from google import genai
from google.genai import types

from core.safety import ContentBlocked
from core.usage import TokenUsage

# Strictest threshold on every category: block anything rated LOW or above. Gemini has no separate
# violence category: threats fall under harassment, and how-to-harm under dangerous content.
# Tested against banking questions about theft, threats, anger, fraud and lost cards: none blocked.
SAFETY_SETTINGS = [
    types.SafetySetting(category=category, threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE)
    for category in (
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
]

# Finish reasons that mean the answer was stopped by a filter, not finished.
BLOCKED_FINISH_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}

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
            safety_settings=SAFETY_SETTINGS,
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
            feedback = chunk.prompt_feedback
            if feedback is not None and feedback.block_reason is not None:
                raise ContentBlocked("input", (feedback.block_reason.name,))
            if chunk.text:
                yield chunk.text
            for candidate in chunk.candidates or []:
                reason = candidate.finish_reason
                if reason is not None and reason.name in BLOCKED_FINISH_REASONS:
                    raise ContentBlocked("output", _flagged_categories(candidate) or (reason.name,))


def _flagged_categories(candidate: types.Candidate) -> tuple[str, ...]:
    """The categories Gemini blocked on, or rated above negligible, as short names."""
    ratings = candidate.safety_ratings or []
    flagged = [
        r.category.name.removeprefix("HARM_CATEGORY_")
        for r in ratings
        if r.category is not None
        and (r.blocked or (r.probability is not None and r.probability.name != "NEGLIGIBLE"))
    ]
    return tuple(sorted(set(flagged)))
