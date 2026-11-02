"""ChunkStore in memory, for tests and local runs without a database."""

from collections.abc import Collection, Sequence

from core.indexing import StoredChunk


class MemoryChunkStore:
    def __init__(self) -> None:
        self.rows: dict[str, StoredChunk] = {}

    def content_hashes(self) -> set[str]:
        return set(self.rows)

    def apply(self, insert: Sequence[StoredChunk], delete: Collection[str]) -> None:
        if overlap := {row.content_hash for row in insert} & set(self.rows):
            raise ValueError(f"already stored: {sorted(overlap)[:3]}")  # as the unique key would
        for content_hash in delete:
            self.rows.pop(content_hash, None)
        for row in insert:
            self.rows[row.content_hash] = row
