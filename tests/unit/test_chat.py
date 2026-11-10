"""core.chat with retrieval (ADR 0007, #88): sources first, then one model call with the context."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from adapters.shared.hash_embedder import HashEmbedder
from adapters.shared.memory_chunks import MemoryChunkStore
from adapters.shared.memory_knowledge import MemoryKnowledgeStore
from adapters.shared.memory_usage import MemoryUsageSink
from core.chat import (
    SYSTEM_PROMPT,
    ChatService,
    KnowledgeUnavailable,
    Source,
    Sources,
    sources_for,
    user_message,
)
from core.indexing import index
from core.knowledge import load_docs, load_markets
from core.redaction import unredacted
from core.retrieval import RetrievedChunk, Retriever
from core.usage import TokenUsage

KNOWLEDGE = Path(__file__).resolve().parents[2] / "knowledge"
MARKETS = load_markets(KNOWLEDGE / "markets.yaml")
QUESTION = "¿Qué documentos necesito para abrir una cuenta?"


class RecordingLLM:
    model = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def stream(
        self, system: str, user_message: str, usage: TokenUsage | None = None, history: object = ()
    ) -> AsyncIterator[str]:
        self.calls.append((system, user_message))
        yield "Respuesta."


def retriever(min_score: float = 0.0) -> Retriever:
    embedder, chunks = HashEmbedder(), MemoryChunkStore()
    index(load_docs(KNOWLEDGE / "docs"), embedder, chunks)
    return Retriever(embedder, MemoryKnowledgeStore(chunks.rows.values()), MARKETS, min_score)


def run(service: ChatService, message: str, market: str | None) -> list[str | Sources]:
    async def consume() -> list[str | Sources]:
        return [event async for event in service.reply(unredacted(message), market)]

    return asyncio.run(consume())


def chunk(title: str, heading: str, market: str = "MX", content: str = "Texto.") -> RetrievedChunk:
    return RetrievedChunk("doc", market, title, (heading,), content, 0.9)


def test_sources_come_first_then_one_call_with_the_context_after_the_system_prompt() -> None:
    llm = RecordingLLM()
    events = run(ChatService(llm, retriever=retriever()), QUESTION, "MX")

    assert isinstance(events[0], Sources) and events[0].sources
    assert events[1:] == ["Respuesta."]
    [(system, prompt)] = llm.calls
    assert system == SYSTEM_PROMPT  # the cached prefix never changes
    assert prompt.startswith("Fuentes:\n\n[1] ")
    assert prompt.endswith(f"Pregunta del cliente: {QUESTION}")


def test_each_market_gets_only_its_own_and_shared_sources() -> None:
    service = ChatService(RecordingLLM(), retriever=retriever())
    us, mx = (run(service, QUESTION, market)[0] for market in ("US", "MX"))

    assert isinstance(us, Sources) and isinstance(mx, Sources)
    assert {s.market for s in us.sources} <= {"US", "all"}
    assert {s.market for s in mx.sources} <= {"MX", "all"}
    assert us.sources != mx.sources


def test_nothing_above_the_threshold_tells_the_model_there_are_no_sources() -> None:
    llm = RecordingLLM()
    events = run(ChatService(llm, retriever=retriever(min_score=1.01)), QUESTION, "MX")

    assert events[0] == Sources(())
    assert llm.calls[0][1] == f"Fuentes: ninguna.\n\nPregunta del cliente: {QUESTION}"


def test_without_a_market_the_model_answers_alone() -> None:
    llm = RecordingLLM()
    assert run(ChatService(llm, retriever=retriever()), "hola", None) == ["Respuesta."]
    assert llm.calls == [(SYSTEM_PROMPT, "hola")]


def test_usage_records_the_context_part_only_when_retrieved() -> None:
    usage = MemoryUsageSink()
    service = ChatService(RecordingLLM(), usage=usage, retriever=retriever())
    run(service, QUESTION, "MX")
    run(service, QUESTION, None)

    assert [r.prompt_parts for r in usage.records] == [
        ("system", "context", "user"),
        ("system", "user"),
    ]


def test_a_failed_retrieval_stops_before_the_model_and_hides_the_cause() -> None:
    class BrokenStore:
        def search(
            self, vector: Sequence[float], markets: Sequence[str], k: int, model: str
        ) -> list[RetrievedChunk]:
            raise ConnectionError("detail that could quote the question")

    llm, usage = RecordingLLM(), MemoryUsageSink()
    service = ChatService(
        llm, usage=usage, retriever=Retriever(HashEmbedder(), BrokenStore(), MARKETS)
    )
    with pytest.raises(KnowledgeUnavailable) as raised:
        run(service, QUESTION, "MX")

    assert str(raised.value) == ""
    assert isinstance(raised.value.__cause__, ConnectionError)
    assert llm.calls == [] and usage.records == []


def test_sources_name_each_section_once_in_retrieval_order() -> None:
    chunks = [
        chunk("Cuenta", "Requisitos"),
        chunk("Tarjeta", "Bloqueo"),
        chunk("Cuenta", "Requisitos"),
    ]
    assert sources_for(chunks) == Sources(
        (Source("Cuenta", "Requisitos", "MX"), Source("Tarjeta", "Bloqueo", "MX"))
    )


def test_the_prompt_numbers_each_chunk_with_its_title_and_headings() -> None:
    prompt = user_message("¿Y?", [chunk("Cuenta", "Requisitos", content="Trae tu INE.")])
    assert (
        prompt == "Fuentes:\n\n[1] Cuenta — Requisitos\nTrae tu INE.\n\nPregunta del cliente: ¿Y?"
    )
