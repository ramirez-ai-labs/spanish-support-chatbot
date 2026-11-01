"""UsageSink that inserts into llm_usage in Neon, as the insert-only usage_writer role (ADR 0006).

One connection, opened on the first record and reused; a broken one is dropped and reopened on the
next record. Writes run after the answer has streamed, and a failure is logged by error type and
swallowed: losing a usage row must never fail a chat.
"""

import asyncio
import contextlib
import logging

import psycopg

from core.usage import LLMUsage

logger = logging.getLogger(__name__)

# Generous: a Neon compute that scaled to zero takes a few seconds to wake up.
WRITE_TIMEOUT_SECONDS = 10

INSERT = """
insert into llm_usage (
    cloud, model, purpose, prompt_parts, input_tokens, cached_tokens, output_tokens,
    thinking_tokens, latency_ms, ttft_ms, outcome, trace_id
) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""  # no RETURNING: usage_writer can't read rows back


class NeonUsageSink:
    def __init__(self, url: str) -> None:
        self._url = url
        self._conn: psycopg.AsyncConnection[tuple[object, ...]] | None = None
        self._lock = asyncio.Lock()  # one connection, so one insert at a time

    async def record(self, usage: LLMUsage) -> None:
        try:
            await asyncio.wait_for(self._insert(usage), WRITE_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("usage_write_failed type=%s", type(exc).__name__)
            await self._drop_connection()

    async def _insert(self, usage: LLMUsage) -> None:
        async with self._lock:
            if self._conn is None or self._conn.closed:
                self._conn = await psycopg.AsyncConnection.connect(
                    self._url, autocommit=True, connect_timeout=WRITE_TIMEOUT_SECONDS
                )
            t = usage.tokens
            await self._conn.execute(
                INSERT,
                (
                    usage.cloud,
                    usage.model,
                    usage.purpose,
                    list(usage.prompt_parts),
                    t.input_tokens,
                    t.cached_tokens,
                    t.output_tokens,
                    t.thinking_tokens,
                    usage.latency_ms,
                    usage.ttft_ms,
                    usage.outcome,
                    usage.trace_id,
                ),
            )

    async def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):  # already broken; nothing else to do
                await conn.close()
