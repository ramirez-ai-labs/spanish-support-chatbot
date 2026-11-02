"""KnowledgeStore over Neon's `chunks` table, as the read-only `app_gcp` role (ADR 0007).

pgvector filters *after* an HNSW scan, so a market filter could quietly return fewer chunks than
asked for. Each query turns on iterative scans (pgvector 0.8), which keep scanning until enough
rows pass the filter; their order is only approximately by distance, so results are re-sorted.
One connection is reused, and reopened once if Neon closed it while its compute was idle.
"""

import contextlib
import threading
from collections.abc import Sequence
from typing import Any

import psycopg

from core.retrieval import RetrievedChunk

QUERY = """
select doc_id, market, title, heading_path, content, 1 - (embedding <=> %(v)s::vector) as score
from chunks
where market = any(%(markets)s) and embedding_model = %(model)s
order by embedding <=> %(v)s::vector
limit %(k)s
"""


class NeonKnowledgeStore:
    def __init__(self, url: str, connect_timeout: int = 15) -> None:
        self._url = url
        self._connect_timeout = connect_timeout
        self._conn: psycopg.Connection[tuple[object, ...]] | None = None
        self._lock = threading.Lock()  # requests run in worker threads; one connection

    def search(
        self, vector: Sequence[float], markets: Sequence[str], k: int, model: str
    ) -> list[RetrievedChunk]:
        if not markets:
            raise ValueError("search needs at least one market")
        params = {"v": _vector(vector), "markets": list(markets), "model": model, "k": k}
        with self._lock:
            try:
                rows = self._query(params)
            except psycopg.OperationalError:
                self._close()  # most likely closed by Neon while idle: reconnect once
                rows = self._query(params)
        hits = [
            RetrievedChunk(str(r[0]), str(r[1]), str(r[2]), tuple(r[3]), str(r[4]), float(r[5]))
            for r in rows
        ]
        return sorted(hits, key=lambda hit: hit.score, reverse=True)

    def _query(self, params: dict[str, object]) -> list[tuple[Any, ...]]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._url, connect_timeout=self._connect_timeout)
        with self._conn.transaction():
            self._conn.execute("set local hnsw.iterative_scan = relaxed_order")
            return self._conn.execute(QUERY, params).fetchall()

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(psycopg.Error):  # already broken
                conn.close()


def _vector(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{v:.7g}" for v in values) + "]"
