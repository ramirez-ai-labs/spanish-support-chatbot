"""Indexes knowledge/ into a chunk store: thin entry point; the logic is core.indexing.

    INDEXER_DATABASE_URL=... EMBEDDER=e5 E5_MODEL_DIR=... uv run python -m apps.indexer

The connection string comes from the environment (in CI, from Secret Manager as indexer-ci), never
from an argument, so it stays out of process lists and shell history. Prints counts only.
"""

import os
import sys
from pathlib import Path

from core.indexing import index
from core.knowledge import load_docs, load_markets
from core.ports import Embedder

KNOWLEDGE = Path(__file__).resolve().parents[2] / "knowledge"


def main() -> int:
    url = os.environ.get("INDEXER_DATABASE_URL")
    if not url:
        print("Set INDEXER_DATABASE_URL (the indexer role's connection string).", file=sys.stderr)
        return 2
    from adapters.shared.neon_chunks import NeonChunkStore

    embedder: Embedder
    choice = os.environ.get("EMBEDDER", "e5")
    if choice == "e5":
        from adapters.shared.e5 import E5Embedder

        embedder = E5Embedder(Path(os.environ["E5_MODEL_DIR"]))
    elif choice == "hash":  # tests and local runs without the model
        from adapters.shared.hash_embedder import HashEmbedder

        embedder = HashEmbedder()
    else:
        print(f"Unknown EMBEDDER {choice!r}: indexing needs e5 (or hash).", file=sys.stderr)
        return 2

    markets = load_markets(KNOWLEDGE / "markets.yaml")
    docs = load_docs(KNOWLEDGE / "docs")
    unknown = {doc.market for doc in docs} - set(markets) - {"all"}
    if unknown:
        print(f"Docs for unknown markets: {sorted(unknown)}", file=sys.stderr)
        return 1

    result = index(docs, embedder, NeonChunkStore(url))
    print(
        f"{len(docs)} docs, {result.chunks} chunks ({embedder.model}): "
        f"{result.embedded} embedded, {result.deleted} deleted, {result.unchanged} unchanged"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
