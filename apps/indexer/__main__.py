"""Indexes knowledge/ into a chunk store, or checks one: thin entry point; the logic is in core.

    INDEXER_DATABASE_URL=... EMBEDDER=e5 E5_MODEL_DIR=... uv run python -m apps.indexer [check]

`check` runs core.index_checks on the store (as the same role: `indexer` can read `chunks`) and
exits 1 if any check fails; the indexing workflow runs it before promoting to `main` (#86). The
connection string comes from the environment (in CI, from Secret Manager as indexer-ci), never
from an argument, so it stays out of process lists and shell history. Prints counts, doc IDs and
scores only, as Markdown for the workflow's run summary.
"""

import os
import sys
from pathlib import Path

from core.index_checks import CheckReport, check_index, load_smoke_set
from core.indexing import index
from core.knowledge import load_docs, load_markets
from core.ports import Embedder

KNOWLEDGE = Path(__file__).resolve().parents[2] / "knowledge"


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "index"
    if command not in ("index", "check"):
        print(f"Unknown command {command!r}: index (the default) or check.", file=sys.stderr)
        return 2
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

    if command == "check":
        from adapters.shared.neon_knowledge import NeonKnowledgeStore

        smoke = load_smoke_set(KNOWLEDGE / "smoke.yaml")
        report = check_index(
            docs, markets, embedder, NeonChunkStore(url), NeonKnowledgeStore(url), smoke
        )
        print(render(report))
        return 0 if report.passed else 1

    result = index(docs, embedder, NeonChunkStore(url))
    print(
        f"{len(docs)} docs, {result.chunks} chunks ({embedder.model}): "
        f"{result.embedded} embedded, {result.deleted} deleted, {result.unchanged} unchanged"
    )
    return 0


def render(report: CheckReport) -> str:
    lines = ["**Checks passed.**" if report.passed else "**Checks failed:**"]
    lines += [f"- {failure}" for failure in report.failures]
    counts = ", ".join(f"{m} {n}" for m, n in sorted(report.market_counts.items()))
    lines += ["", f"Chunks per market: {counts or 'none'}"]
    if report.smoke:
        lines += ["", "| Market | Question | Expected | Rank | Score |", "|---|---|---|---|---|"]
        lines += [
            f"| {r.question.market} | {r.question.question} | {r.question.expect} | "
            f"{r.rank or '❌ first: ' + (r.found[0] if r.found else 'nothing')} | {r.score:.3f} |"
            for r in report.smoke
        ]
    if report.out_of_scope:
        lines += ["", "| Out of scope | Best score |", "| --- | --- |"]
        lines += [f"| {question} | {score:.3f} |" for question, score in report.out_of_scope]
    low, high = report.lowest_covered, report.highest_uncovered
    if low is not None and high is not None:
        lines += [
            "",
            f"Lowest covered score {low:.3f}, highest out-of-scope score {high:.3f}: "
            f'a gap of {low - high:+.3f} for the "I don\'t know" threshold.',
        ]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
