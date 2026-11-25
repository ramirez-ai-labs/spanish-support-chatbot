"""CardStore over Neon's `bank.cards` (migration 0005), as the read-only `app_gcp` role.

The query is by owner only, so a customer's own `sub` is the only way to any card. One connection
is reused, and reopened once if Neon closed it while its compute was idle.
"""

import contextlib
import threading
from typing import Any

import psycopg

from core.cards import Card

QUERY = """
select last4, kind, status, expires_on, market
from bank.cards
where owner_sub = %s
order by kind, last4
"""


class NeonCardStore:
    def __init__(self, url: str, connect_timeout: int = 15) -> None:
        self._url = url
        self._connect_timeout = connect_timeout
        self._conn: psycopg.Connection[tuple[Any, ...]] | None = None
        self._lock = threading.Lock()  # tools run in worker threads; one connection

    def cards_of(self, sub: str) -> list[Card]:
        with self._lock:
            try:
                rows = self._query(sub)
            except psycopg.OperationalError:
                self._close()  # most likely closed by Neon while idle: reconnect once
                rows = self._query(sub)
        return [Card(str(r[0]), str(r[1]), str(r[2]), r[3], str(r[4])) for r in rows]

    def _query(self, sub: str) -> list[tuple[Any, ...]]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._url, connect_timeout=self._connect_timeout, autocommit=True
            )
        return self._conn.execute(QUERY, (sub,)).fetchall()

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(psycopg.Error):  # already broken
                conn.close()
