"""core.indexing with the in-memory store: embeds only what's new, and converges on every run."""

import dataclasses
import datetime as dt
from collections.abc import Sequence

from adapters.shared.hash_embedder import HashEmbedder
from adapters.shared.memory_chunks import MemoryChunkStore
from core.indexing import IndexResult, index
from core.knowledge import Doc


class CountingEmbedder(HashEmbedder):
    def __init__(self, model: str = "hash-test") -> None:
        self.model = model
        self.embedded: list[str] = []

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded += texts
        return super().embed_passages(texts)


def doc(doc_id: str, body: str, market: str = "MX") -> Doc:
    return Doc(doc_id, market, doc_id, "t", dt.date(2026, 10, 1), body)


DOCS = [
    doc("a", "## Uno\n\nTexto uno.\n\n## Dos\n\nTexto dos."),
    doc("b", "## Tres\n\nTexto tres."),
]


def test_the_first_run_embeds_every_chunk() -> None:
    store, embedder = MemoryChunkStore(), CountingEmbedder()
    assert index(DOCS, embedder, store) == IndexResult(3, 3, 0, 0)
    assert len(store.rows) == 3 and len(embedder.embedded) == 3
    row = next(iter(store.rows.values()))
    assert row.embedding_model == "hash-test" and len(row.embedding) == embedder.dimensions


def test_a_rerun_with_nothing_changed_embeds_nothing() -> None:
    store = MemoryChunkStore()
    index(DOCS, CountingEmbedder(), store)
    embedder = CountingEmbedder()
    assert index(DOCS, embedder, store) == IndexResult(3, 0, 0, 3)
    assert embedder.embedded == []


def test_an_edited_section_is_replaced_and_the_rest_kept() -> None:
    store = MemoryChunkStore()
    index(DOCS, CountingEmbedder(), store)
    edited = [doc("a", "## Uno\n\nTexto uno.\n\n## Dos\n\nTexto nuevo."), DOCS[1]]
    embedder = CountingEmbedder()
    assert index(edited, embedder, store) == IndexResult(3, 1, 1, 2)
    assert embedder.embedded == ["a — Dos\n\nTexto nuevo."]
    assert sorted(r.chunk.content for r in store.rows.values()) == [
        "Texto nuevo.",
        "Texto tres.",
        "Texto uno.",
    ]


def test_a_removed_doc_loses_its_chunks() -> None:
    store = MemoryChunkStore()
    index(DOCS, CountingEmbedder(), store)
    assert index(DOCS[:1], CountingEmbedder(), store) == IndexResult(2, 0, 1, 2)
    assert {r.chunk.doc_id for r in store.rows.values()} == {"a"}


def test_a_new_model_re_embeds_everything_and_drops_old_vectors() -> None:
    store = MemoryChunkStore()
    index(DOCS, CountingEmbedder("model-1"), store)
    assert index(DOCS, CountingEmbedder("model-2"), store) == IndexResult(3, 3, 3, 0)
    assert {r.embedding_model for r in store.rows.values()} == {"model-2"}


def test_a_moved_doc_is_re_embedded_under_its_new_market() -> None:
    store = MemoryChunkStore()
    index(DOCS, CountingEmbedder(), store)
    moved = [DOCS[0], dataclasses.replace(DOCS[1], market="SV")]
    assert index(moved, CountingEmbedder(), store).embedded == 1
    assert {r.chunk.market for r in store.rows.values() if r.chunk.doc_id == "b"} == {"SV"}
