"""core.retrieval: the market check, the query embedding, and the score threshold."""

from collections.abc import Sequence

import pytest

from adapters.shared.hash_embedder import HashEmbedder
from core.retrieval import RetrievedChunk, Retriever, UnknownMarket


class RecordingEmbedder(HashEmbedder):
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.passages = 0

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return super().embed_query(text)

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.passages += len(texts)
        return super().embed_passages(texts)


class RecordingStore:
    def __init__(self, scores: Sequence[float] = (0.9, 0.85, 0.7)) -> None:
        self.calls: list[tuple[list[str], int, str]] = []
        self.scores = scores

    def search(
        self, vector: Sequence[float], markets: Sequence[str], k: int, model: str
    ) -> list[RetrievedChunk]:
        self.calls.append((list(markets), k, model))
        return [RetrievedChunk("d", markets[0], "t", ("h",), "c", s) for s in self.scores]


def test_searches_the_market_plus_all_with_the_query_embedding() -> None:
    embedder, store = RecordingEmbedder(), RecordingStore()
    Retriever(embedder, store, {"MX", "US"}, k=4).retrieve("¿Cuánto cuesta?", "MX")
    assert store.calls == [(["MX", "all"], 4, embedder.model)]
    assert embedder.queries == ["¿Cuánto cuesta?"] and embedder.passages == 0


@pytest.mark.parametrize("market", ["CO", "mx", "all", ""])
def test_an_unknown_market_is_refused_before_any_search(market: str) -> None:
    store = RecordingStore()
    with pytest.raises(UnknownMarket):
        Retriever(HashEmbedder(), store, {"MX", "US"}).retrieve("hola", market)
    assert store.calls == []


def test_chunks_below_the_threshold_are_dropped() -> None:
    retriever = Retriever(HashEmbedder(), RecordingStore(), {"MX"}, min_score=0.8)
    assert [hit.score for hit in retriever.retrieve("hola", "MX")] == [0.9, 0.85]


def test_nothing_above_the_threshold_means_no_sources() -> None:
    retriever = Retriever(HashEmbedder(), RecordingStore((0.6, 0.5)), {"MX"}, min_score=0.8)
    assert retriever.retrieve("¿Cuál es la capital de Francia?", "MX") == []
