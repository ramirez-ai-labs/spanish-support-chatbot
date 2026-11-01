"""LLM usage records: one per model call, written to llm_usage (migrations/0001_usage.sql).

Tokens come from the provider's own counts, filled in by the LLMProvider adapter. Everything else
(timing, outcome, purpose) is measured by the core. A record never holds message text, only the
names of the prompt's parts. Cost isn't here: it's computed at query time from dated prices.
"""

from dataclasses import dataclass
from typing import Literal

Purpose = Literal["chat", "eval", "index"]
Outcome = Literal["done", "error", "incomplete"]


@dataclass
class TokenUsage:
    """Filled in by an LLMProvider while it streams; zero until the provider reports counts."""

    input_tokens: int = 0  # the whole prompt, including cached_tokens
    cached_tokens: int = 0
    output_tokens: int = 0  # the visible answer, without thinking
    thinking_tokens: int = 0


@dataclass(frozen=True)
class LLMUsage:
    cloud: str
    model: str
    purpose: Purpose
    prompt_parts: tuple[str, ...]
    tokens: TokenUsage
    latency_ms: int
    ttft_ms: int | None
    outcome: Outcome
    trace_id: str | None
