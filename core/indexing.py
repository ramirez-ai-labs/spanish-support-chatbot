"""Keeps the chunk store in step with the knowledge docs (ADR 0007, #85).

Chunks are keyed by their content hash, so a run embeds only what's new or changed, deletes what
no doc produces any more (removed docs, edited sections, an old model's vectors), and is a no-op
when nothing changed. Rerunning is always safe.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from core.chunking import Chunk, chunk_doc
from core.knowledge import Doc
from core.ports import ChunkStore, Embedder


@dataclass(frozen=True)
class StoredChunk:
    chunk: Chunk
    content_hash: str
    embedding: list[float]
    embedding_model: str


@dataclass(frozen=True)
class IndexResult:
    chunks: int  # how many chunks the docs produce
    embedded: int  # new or changed, embedded this run
    deleted: int  # in the store but no longer produced
    unchanged: int


def index(docs: Sequence[Doc], embedder: Embedder, store: ChunkStore) -> IndexResult:
    wanted: dict[str, Chunk] = {}
    for doc in docs:
        for chunk in chunk_doc(doc):
            wanted.setdefault(chunk.content_hash(embedder.model), chunk)

    existing = store.content_hashes()
    new_hashes = [h for h in wanted if h not in existing]
    vectors = embedder.embed_passages([wanted[h].passage() for h in new_hashes])
    new = [
        StoredChunk(wanted[h], h, vector, embedder.model)
        for h, vector in zip(new_hashes, vectors, strict=True)
    ]
    stale = existing - wanted.keys()
    store.apply(insert=new, delete=stale)
    return IndexResult(
        chunks=len(wanted),
        embedded=len(new),
        deleted=len(stale),
        unchanged=len(wanted) - len(new),
    )
