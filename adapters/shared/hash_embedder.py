"""Embedder for tests and local runs without the model: deterministic, instant, no files.

Each word is hashed into one of the vector's slots, so texts that share words point the same
way. That's enough to test indexing and the market filter; it knows nothing about meaning.
"""

import hashlib
import math
import re
from collections.abc import Sequence

DIMENSIONS = 384  # the same as e5-small, so the same table works


class HashEmbedder:
    model = "hash-embedder-v1"
    dimensions = DIMENSIONS

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * DIMENSIONS
        for word in re.findall(r"\w+", text.lower()):
            slot = int.from_bytes(hashlib.blake2b(word.encode(), digest_size=4).digest(), "big")
            vector[slot % DIMENSIONS] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        if not any(vector):
            vector[0] = 1.0  # empty text: still a unit vector
            return vector
        return [v / norm for v in vector]
