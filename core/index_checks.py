"""Checks an indexed chunk store before it's promoted (ADR 0007, #86).

The indexing workflow fills Neon's `dev` branch, runs these checks on it, and fills `main` only if
they all pass; then it checks `main` the same way. They are:
- the store holds exactly the chunks the docs produce: the run converged, with nothing left over
- every market has chunks, and no chunk is in a market outside knowledge/markets.yaml
- each question in knowledge/smoke.yaml finds a chunk of its expected doc among the k chunks the
  API would give the model (e5's scores bunch, so requiring first place would fail on near-ties)

The out-of-scope questions are asked too, in every market, and their best scores are reported,
not checked: the gap between those and the covered questions' scores calibrates the "I don't
know" threshold. Reports hold doc IDs and scores, never chunk text.
"""

from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from core.indexing import wanted_chunks
from core.knowledge import ALL, Doc
from core.ports import ChunkStore, Embedder, KnowledgeStore
from core.retrieval import DEFAULT_K, Retriever


@dataclass(frozen=True)
class SmokeQuestion:
    market: str
    question: str
    expect: str  # the doc_id one of whose chunks must be found


@dataclass(frozen=True)
class SmokeSet:
    questions: tuple[SmokeQuestion, ...]
    out_of_scope: tuple[str, ...]


@dataclass(frozen=True)
class SmokeResult:
    question: SmokeQuestion
    found: tuple[str, ...]  # the doc_id of each chunk found, best first
    score: float  # the best score of an expected doc's chunk; 0.0 when none was found

    @property
    def rank(self) -> int | None:
        """Where the expected doc's first chunk came, from 1; None when it wasn't found."""
        expect = self.question.expect
        return self.found.index(expect) + 1 if expect in self.found else None

    @property
    def passed(self) -> bool:
        return self.rank is not None


@dataclass(frozen=True)
class CheckReport:
    failures: tuple[str, ...]
    market_counts: dict[str, int]
    smoke: tuple[SmokeResult, ...]
    out_of_scope: tuple[tuple[str, float], ...]  # each question's best score over every market

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def lowest_covered(self) -> float | None:
        """The lowest score among the smoke questions that found their doc."""
        return min((r.score for r in self.smoke if r.passed), default=None)

    @property
    def highest_uncovered(self) -> float | None:
        return max((score for _, score in self.out_of_scope), default=None)


def load_smoke_set(path: Path) -> SmokeSet:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    questions = tuple(
        SmokeQuestion(str(q["market"]), str(q["question"]), str(q["expect"]))
        for q in data["questions"]
    )
    return SmokeSet(questions, tuple(str(q) for q in data.get("out_of_scope", [])))


def smoke_set_problems(smoke: SmokeSet, docs: Sequence[Doc], markets: Collection[str]) -> list[str]:
    """What's wrong with the smoke set itself: checked before any question is asked."""
    doc_markets = {doc.doc_id: doc.market for doc in docs}
    problems = []
    for q in smoke.questions:
        if q.market not in markets:
            problems.append(f"smoke question for unknown market {q.market}: {q.question!r}")
        elif q.expect not in doc_markets:
            problems.append(f"smoke question expects unknown doc {q.expect}: {q.question!r}")
        elif doc_markets[q.expect] not in (q.market, ALL):
            problems.append(f"smoke question in {q.market} expects {q.expect}, from another market")
    asked = {q.market for q in smoke.questions}
    problems += [f"no smoke question for market {m}" for m in sorted(set(markets) - asked)]
    return problems


def check_index(
    docs: Sequence[Doc],
    markets: Collection[str],
    embedder: Embedder,
    chunks: ChunkStore,
    knowledge: KnowledgeStore,
    smoke: SmokeSet,
    k: int = DEFAULT_K,
) -> CheckReport:
    failures: list[str] = []
    wanted = wanted_chunks(docs, embedder.model)
    stored = chunks.content_hashes()
    if stored != wanted.keys():
        failures.append(
            f"the store isn't in step with the docs: {len(wanted.keys() - stored)} chunks missing, "
            f"{len(stored - wanted.keys())} left over"
        )

    counts = chunks.market_counts()
    expected = Counter(chunk.market for chunk in wanted.values())
    failures += [f"market {m} has no chunks" for m in sorted(markets) if not counts.get(m)]
    unknown = sorted(set(counts) - set(markets) - {ALL})
    failures += [f"{counts[m]} chunks in unknown market {m}" for m in unknown]
    failures += [
        f"market {m} has {counts.get(m, 0)} chunks, the docs produce {n}"
        for m, n in sorted(expected.items())
        if counts.get(m, 0) != n
    ]

    problems = smoke_set_problems(smoke, docs, markets)
    if problems:
        return CheckReport(tuple(failures + problems), counts, (), ())

    retriever = Retriever(embedder, knowledge, markets, min_score=0.0, k=k)
    results = []
    for q in smoke.questions:
        hits = retriever.retrieve(q.question, q.market)
        score = max((hit.score for hit in hits if hit.doc_id == q.expect), default=0.0)
        results.append(SmokeResult(q, tuple(hit.doc_id for hit in hits), score))
    failures += [
        f"{r.question.market} {r.question.question!r}: expected {r.question.expect} in the top "
        f"{k}, found {', '.join(r.found) or 'nothing'}"
        for r in results
        if not r.passed
    ]

    uncovered = []
    for question in smoke.out_of_scope:
        scores = [hit.score for m in markets for hit in retriever.retrieve(question, m)[:1]]
        uncovered.append((question, max(scores, default=0.0)))

    return CheckReport(tuple(failures), counts, tuple(results), tuple(uncovered))
