"""Retrieval for one question in one market (ADR 0007, #87).

The visitor's market must be one from knowledge/markets.yaml; docs for `all` markets are always
included. Chunks scoring below `min_score` are dropped, so a question the docs don't cover gets
no sources, and the model says it doesn't know instead of guessing. e5's scores bunch together
(roughly 0.8 to 0.95), so the threshold is calibrated on data (#86), not guessed here.
"""

from collections.abc import Collection
from dataclasses import dataclass

from core.knowledge import ALL
from core.ports import Embedder, KnowledgeStore

DEFAULT_K = 4


class UnknownMarket(ValueError):
    """The market isn't in knowledge/markets.yaml. Safe to show: it's the caller's own input."""


@dataclass(frozen=True)
class RetrievedChunk:
    doc_id: str
    market: str
    title: str
    heading_path: tuple[str, ...]
    content: str
    score: float  # cosine similarity, 1.0 is identical


class Retriever:
    def __init__(
        self,
        embedder: Embedder,
        store: KnowledgeStore,
        markets: Collection[str],
        min_score: float = 0.0,  # off until #86 calibrates it
        k: int = DEFAULT_K,
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._markets = frozenset(markets)
        self._min_score = min_score
        self._k = k

    def retrieve(self, question: str, market: str) -> list[RetrievedChunk]:
        if market not in self._markets:
            raise UnknownMarket(market)
        vector = self._embedder.embed_query(question)
        hits = self._store.search(vector, [market, ALL], self._k, self._embedder.model)
        return [hit for hit in hits if hit.score >= self._min_score]
