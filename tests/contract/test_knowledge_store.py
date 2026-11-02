"""Contract for KnowledgeStore: every adapter must pass every test here (ADR 0007).

The rule that matters: a search returns chunks from the given markets and no others. Each adapter
searches the real corpus (knowledge/), indexed with the hash embedder, so these tests need no
model. The Neon adapter runs as the read-only `app_gcp` role against the Postgres from conftest.py.
"""

import math
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from adapters.shared.hash_embedder import HashEmbedder
from adapters.shared.memory_chunks import MemoryChunkStore
from adapters.shared.memory_knowledge import MemoryKnowledgeStore
from adapters.shared.neon_chunks import NeonChunkStore
from adapters.shared.neon_knowledge import NeonKnowledgeStore
from core.chunking import Chunk
from core.indexing import StoredChunk, index
from core.knowledge import ALL, load_docs, load_markets
from core.ports import KnowledgeStore
from tests.contract.conftest import as_role

KNOWLEDGE = Path(__file__).resolve().parents[2] / "knowledge"
MARKETS = sorted(load_markets(KNOWLEDGE / "markets.yaml"))
DOCS = load_docs(KNOWLEDGE / "docs")
EMBEDDER = HashEmbedder()
QUESTIONS = [
    "¿Qué necesito para abrir una cuenta?",
    "Me robaron la tarjeta",
    "¿Cuánto cuesta un retiro en cajero?",
    "¿Cómo recibo dinero de otro banco?",
    "No reconozco un cargo",
    "Quiero hablar con una persona",
]


def memory(rows: list[StoredChunk]) -> KnowledgeStore:
    store = MemoryChunkStore()
    store.apply(insert=rows, delete=[])
    return MemoryKnowledgeStore(store.rows.values())


@pytest.fixture
def neon_factory(database: str) -> Iterator[Callable[[list[StoredChunk]], KnowledgeStore]]:
    def build(rows: list[StoredChunk]) -> KnowledgeStore:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute("truncate chunks")
        NeonChunkStore(as_role(database, "indexer")).apply(insert=rows, delete=[])
        return NeonKnowledgeStore(as_role(database, "app_gcp"))

    yield build


ADAPTERS: list[Any] = [
    pytest.param(lambda request: memory, id="memory"),
    pytest.param(lambda request: request.getfixturevalue("neon_factory"), id="neon"),
]


@pytest.fixture(params=ADAPTERS)
def make(request: pytest.FixtureRequest) -> Callable[[list[StoredChunk]], KnowledgeStore]:
    build: Callable[[pytest.FixtureRequest], Callable[[list[StoredChunk]], KnowledgeStore]]
    build = request.param
    return build(request)


def corpus_rows() -> list[StoredChunk]:
    store = MemoryChunkStore()
    index(DOCS, EMBEDDER, store)
    return list(store.rows.values())


def row(text: str, market: str, model: str = EMBEDDER.model) -> StoredChunk:
    chunk = Chunk(f"{market.lower()}-{abs(hash(text))}", market, "Título", ("Sección",), text)
    vector = EMBEDDER.embed_passages([chunk.passage()])[0]
    return StoredChunk(chunk, chunk.content_hash(model), vector, model)


def test_results_never_come_from_another_market(make: Callable[..., KnowledgeStore]) -> None:
    store = make(corpus_rows())
    for market in MARKETS:
        for question in QUESTIONS:
            hits = store.search(EMBEDDER.embed_query(question), [market, ALL], 10, EMBEDDER.model)
            assert hits, (market, question)
            assert {hit.market for hit in hits} <= {market, ALL}, (market, question)


def test_another_markets_exact_match_is_still_excluded(make: Callable[..., KnowledgeStore]) -> None:
    rows = corpus_rows()
    spain = next(r for r in rows if r.chunk.market == "ES")
    store = make(rows)
    hits = store.search(spain.embedding, ["MX", ALL], 10, EMBEDDER.model)
    assert spain.chunk.content not in {hit.content for hit in hits}
    assert {hit.market for hit in hits} <= {"MX", ALL}


def test_results_are_the_k_best_highest_first(make: Callable[..., KnowledgeStore]) -> None:
    store = make(corpus_rows())
    hits = store.search(EMBEDDER.embed_query(QUESTIONS[0]), ["SV", ALL], 4, EMBEDDER.model)
    assert len(hits) == 4
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(-1.0 - 1e-6 <= s <= 1.0 + 1e-6 for s in scores)


def test_a_small_market_still_gets_k_results_among_a_big_one(
    make: Callable[..., KnowledgeStore],
) -> None:
    # The filtered-recall trap from ADR 0007: an approximate index scans a few nearest rows, then
    # filters. With 300 US rows and 6 SV rows, a naive scan returns fewer than k SV rows.
    rows = [row(f"Regla de Estados Unidos número {i}", "US") for i in range(300)]
    rows += [row(f"Regla de El Salvador número {i}", "SV") for i in range(6)]
    store = make(rows)
    hits = store.search(EMBEDDER.embed_query("Regla de Estados Unidos"), ["SV"], 4, EMBEDDER.model)
    assert len(hits) == 4
    assert {hit.market for hit in hits} == {"SV"}


def test_only_the_given_models_vectors_are_compared(make: Callable[..., KnowledgeStore]) -> None:
    store = make([row("Comisiones", "MX"), row("Comisiones viejas", "MX", model="old-model")])
    hits = store.search(EMBEDDER.embed_query("Comisiones"), ["MX"], 10, EMBEDDER.model)
    assert [hit.content for hit in hits] == ["Comisiones"]


def test_an_empty_market_list_is_refused(make: Callable[..., KnowledgeStore]) -> None:
    store = make([row("Comisiones", "MX")])
    with pytest.raises(ValueError):
        store.search(EMBEDDER.embed_query("Comisiones"), [], 4, EMBEDDER.model)


def test_hits_carry_what_a_citation_needs(make: Callable[..., KnowledgeStore]) -> None:
    store = make(corpus_rows())
    (hit,) = store.search(EMBEDDER.embed_query(QUESTIONS[0]), ["MX", ALL], 1, EMBEDDER.model)
    assert hit.doc_id and hit.title and hit.heading_path and hit.content
    assert not math.isnan(hit.score)


def test_the_app_role_can_only_read_chunks(neon_factory: Any, database: str) -> None:
    neon_factory([row("Comisiones", "MX")])
    with psycopg.connect(as_role(database, "app_gcp")) as conn:
        for statement in ("delete from chunks", "select count(*) from llm_usage"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()
