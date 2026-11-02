"""Contract for ChunkStore: every adapter must pass every test here.

The Neon adapter runs against a real Postgres with pgvector when TEST_DATABASE_URL points at one
(a superuser on a throwaway database: CI's service container, or `docker compose up db` locally).
The fixture applies the migrations, lets the `indexer` role log in (the test database trusts local
connections, so no password exists anywhere), and runs the adapter as that role, so the grants from
migration 0004 are tested too. Without it, those skip.
"""

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from adapters.shared.hash_embedder import HashEmbedder
from adapters.shared.memory_chunks import MemoryChunkStore
from adapters.shared.neon_chunks import NeonChunkStore
from core.chunking import Chunk
from core.indexing import StoredChunk
from core.ports import ChunkStore

ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get("TEST_DATABASE_URL", "")


def as_role(url: str, role: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=f"{role}@{host}"))


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    if not ADMIN_URL:
        pytest.skip("TEST_DATABASE_URL is not set; start one with `docker compose up -d db`")
    subprocess.run(
        [sys.executable, str(ROOT / "migrations" / "migrate.py")],
        env={**os.environ, "MIGRATIONS_DATABASE_URL": ADMIN_URL},
        check=True,
        capture_output=True,
    )
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute("alter role indexer with login")
    yield ADMIN_URL


@pytest.fixture
def neon(database: str) -> NeonChunkStore:
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute("truncate chunks")
    return NeonChunkStore(as_role(database, "indexer"))


ADAPTERS: list[Any] = [
    pytest.param(lambda request: MemoryChunkStore(), id="memory"),
    pytest.param(lambda request: request.getfixturevalue("neon"), id="neon"),
]


@pytest.fixture(params=ADAPTERS)
def store(request: pytest.FixtureRequest) -> ChunkStore:
    build: Callable[[pytest.FixtureRequest], ChunkStore] = request.param
    return build(request)


def row(text: str, market: str = "MX") -> StoredChunk:
    chunk = Chunk(f"{market.lower()}-doc", market, "Título", ("Sección",), text)
    vector = HashEmbedder().embed_passages([chunk.passage()])[0]
    return StoredChunk(chunk, chunk.content_hash("hash-test"), vector, "hash-test")


def test_inserted_chunks_are_found_by_hash(store: ChunkStore) -> None:
    a, b = row("Uno."), row("Dos.", market="all")
    store.apply(insert=[a, b], delete=[])
    assert store.content_hashes() == {a.content_hash, b.content_hash}


def test_delete_removes_only_the_named_hashes(store: ChunkStore) -> None:
    a, b = row("Uno."), row("Dos.")
    store.apply(insert=[a, b], delete=[])
    store.apply(insert=[], delete=[a.content_hash, "not-stored"])
    assert store.content_hashes() == {b.content_hash}


def test_a_failed_apply_changes_nothing(store: ChunkStore) -> None:
    a, b = row("Uno."), row("Dos.")
    store.apply(insert=[a], delete=[])
    with pytest.raises((ValueError, psycopg.errors.UniqueViolation)):  # a stored hash again
        store.apply(insert=[b, a], delete=[a.content_hash + "x"])
    assert store.content_hashes() == {a.content_hash}


def test_the_indexer_role_can_touch_only_chunks(neon: NeonChunkStore, database: str) -> None:
    with psycopg.connect(as_role(database, "indexer")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select count(*) from llm_usage")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("create table sneaky (id int)")


def test_vectors_round_trip_as_384_dimensions(neon: NeonChunkStore, database: str) -> None:
    a = row("Uno.")
    neon.apply(insert=[a], delete=[])
    with psycopg.connect(database) as conn:
        found = conn.execute(
            "select vector_dims(embedding), embedding_model, heading_path from chunks"
        ).fetchone()
    assert found == (384, "hash-test", ["Sección"])
