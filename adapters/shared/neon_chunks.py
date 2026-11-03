"""ChunkStore in Neon's `chunks` table, as the `indexer` role (migration 0004, ADR 0007).

The role can read and write `chunks` and nothing else. Inserts and deletes for one run happen in a
single transaction, so the API never sees a doc half re-indexed.
"""

from collections.abc import Collection, Sequence
from typing import Any

import psycopg

from core.indexing import StoredChunk

INSERT = """
insert into chunks (
    doc_id, market, title, heading_path, content, content_hash, embedding, embedding_model
) values (%s, %s, %s, %s, %s, %s, %s::vector, %s)
"""


class NeonChunkStore:
    def __init__(self, url: str, connect_timeout: int = 30) -> None:
        self._url = url
        self._connect_timeout = connect_timeout

    def _connect(self) -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(self._url, connect_timeout=self._connect_timeout)

    def content_hashes(self) -> set[str]:
        with self._connect() as conn:
            return {str(row[0]) for row in conn.execute("select content_hash from chunks")}

    def market_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            query = "select market, count(*) from chunks group by market"
            rows: list[tuple[Any, ...]] = conn.execute(query).fetchall()
        return {str(market): int(count) for market, count in rows}

    def apply(self, insert: Sequence[StoredChunk], delete: Collection[str]) -> None:
        with self._connect() as conn, conn.transaction():
            if delete:
                conn.execute("delete from chunks where content_hash = any(%s)", (list(delete),))
            with conn.cursor() as cursor:
                cursor.executemany(
                    INSERT,
                    [
                        (
                            row.chunk.doc_id,
                            row.chunk.market,
                            row.chunk.title,
                            list(row.chunk.heading_path),
                            row.chunk.content,
                            row.content_hash,
                            _vector(row.embedding),
                            row.embedding_model,
                        )
                        for row in insert
                    ],
                )


def _vector(values: list[float]) -> str:
    """pgvector's text form, so no client-side vector adapter is needed."""
    return "[" + ",".join(f"{v:.7g}" for v in values) + "]"
