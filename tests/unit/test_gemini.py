"""GeminiLLM's client settings; its behavior is in the LLMProvider contract suite."""

from typing import Any

import pytest

from adapters.shared import gemini


def test_the_client_has_a_timeout_so_a_stalled_stream_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(gemini.genai, "Client", Client)
    gemini.GeminiLLM(api_key="test-key")

    assert seen["http_options"].timeout == gemini.TIMEOUT_MS
    assert 0 < gemini.TIMEOUT_MS <= 60_000  # well under Cloud Run's 300 s request timeout
