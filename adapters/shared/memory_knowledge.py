"""KnowledgeStore in memory, over the indexer's in-memory chunks: exact search, for tests."""

from collections.abc import Iterable, Sequence

from core.indexing import StoredChunk
from core.retrieval import RetrievedChunk


class MemoryKnowledgeStore:
    def __init__(self, rows: Iterable[StoredChunk]) -> None:
        self._rows = list(rows)

    def search(
        self, vector: Sequence[float], markets: Sequence[str], k: int, model: str
    ) -> list[RetrievedChunk]:
        if not markets:
            raise ValueError("search needs at least one market")
        hits = [
            RetrievedChunk(
                row.chunk.doc_id,
                row.chunk.market,
                row.chunk.title,
                row.chunk.heading_path,
                row.chunk.content,
                sum(a * b for a, b in zip(vector, row.embedding, strict=True)),
            )
            for row in self._rows
            if row.chunk.market in markets and row.embedding_model == model
        ]
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:k]
