"""core.index_checks: what stops a bad index before `main`, with in-memory stores.

The last test runs the real smoke set on the real e5 model when its files are present (CI fetches
them), so a doc change that breaks a smoke question fails its PR, before the indexing workflow.
"""

import dataclasses
import datetime as dt
import os
from pathlib import Path

import pytest

from adapters.shared.hash_embedder import HashEmbedder
from adapters.shared.memory_chunks import MemoryChunkStore
from adapters.shared.memory_knowledge import MemoryKnowledgeStore
from core.index_checks import (
    CheckReport,
    SmokeQuestion,
    SmokeSet,
    check_index,
    load_smoke_set,
    smoke_set_problems,
)
from core.indexing import index
from core.knowledge import Doc, load_docs, load_markets
from core.ports import Embedder

KNOWLEDGE = Path(__file__).resolve().parents[2] / "knowledge"
MARKETS = {"MX", "US"}


def doc(doc_id: str, market: str, body: str) -> Doc:
    return Doc(doc_id, market, doc_id, "t", dt.date(2026, 10, 1), body)


DOCS = [
    doc("mx-tarjeta", "MX", "## Bloqueo\n\nBloquea tu tarjeta perdida desde la app."),
    doc("us-cheques", "US", "## Cheques\n\nDeposita cheques con una foto."),
    doc("all-acceso", "all", "## Contraseña\n\nRecupera tu contraseña olvidada."),
]
SMOKE = SmokeSet(
    questions=(
        SmokeQuestion("MX", "bloquea tarjeta perdida", "mx-tarjeta"),
        SmokeQuestion("US", "deposita cheques foto", "us-cheques"),
        SmokeQuestion("US", "contraseña olvidada", "all-acceso"),
    ),
    out_of_scope=("receta de enchiladas",),
)


def run(
    store: MemoryChunkStore,
    docs: list[Doc] = DOCS,
    markets: set[str] = MARKETS,
    smoke: SmokeSet = SMOKE,
    embedder: Embedder | None = None,
    k: int = 1,  # the toy corpus has two chunks per market search; k=1 makes ranking matter
) -> CheckReport:
    embedder = embedder or HashEmbedder()
    knowledge = MemoryKnowledgeStore(store.rows.values())
    return check_index(docs, markets, embedder, store, knowledge, smoke, k)


def indexed(docs: list[Doc] = DOCS) -> MemoryChunkStore:
    store = MemoryChunkStore()
    index(docs, HashEmbedder(), store)
    return store


def test_a_converged_store_passes_and_reports_counts_and_scores() -> None:
    report = run(indexed())
    assert report.passed, report.failures
    assert report.market_counts == {"MX": 1, "US": 1, "all": 1}
    assert [r.rank for r in report.smoke] == [1, 1, 1]
    assert report.lowest_covered is not None and report.highest_uncovered is not None
    assert report.lowest_covered > report.highest_uncovered


def test_a_store_missing_a_doc_fails() -> None:
    report = run(indexed(DOCS[1:]))
    assert not report.passed
    assert "1 chunks missing, 0 left over" in report.failures[0]
    assert "market MX has no chunks" in report.failures


def test_a_chunk_in_an_unknown_market_fails() -> None:
    store = indexed()
    stray = next(iter(store.rows.values()))
    stray = dataclasses.replace(
        stray,
        chunk=dataclasses.replace(stray.chunk, market="CO"),
        content_hash="stray",
    )
    store.apply(insert=[stray], delete=[])
    failures = run(store).failures
    assert "0 chunks missing, 1 left over" in failures[0]
    assert "1 chunks in unknown market CO" in failures


def test_a_question_that_finds_another_doc_fails() -> None:
    wrong = SmokeQuestion("US", "contraseña olvidada", "us-cheques")
    report = run(indexed(), smoke=SmokeSet(SMOKE.questions + (wrong,), ()))
    assert report.failures == (
        "US 'contraseña olvidada': expected us-cheques in the top 1, found all-acceso",
    )
    assert report.lowest_covered is not None  # the questions that passed still calibrate


@pytest.mark.parametrize(
    ("question", "problem"),
    [
        (SmokeQuestion("CO", "hola", "mx-tarjeta"), "unknown market CO"),
        (SmokeQuestion("MX", "hola", "mx-nada"), "unknown doc mx-nada"),
        (SmokeQuestion("MX", "hola", "us-cheques"), "expects us-cheques, from another market"),
    ],
)
def test_a_broken_smoke_set_asks_no_question(question: SmokeQuestion, problem: str) -> None:
    smoke = SmokeSet(SMOKE.questions + (question,), ())
    report = run(indexed(), smoke=smoke)
    assert any(problem in failure for failure in report.failures)
    assert report.smoke == ()


def test_every_market_needs_a_smoke_question() -> None:
    smoke = SmokeSet(SMOKE.questions[:1], ())
    assert smoke_set_problems(smoke, DOCS, MARKETS) == ["no smoke question for market US"]


def test_the_smoke_set_matches_the_knowledge_base() -> None:
    markets = load_markets(KNOWLEDGE / "markets.yaml")
    docs = load_docs(KNOWLEDGE / "docs")
    smoke = load_smoke_set(KNOWLEDGE / "smoke.yaml")
    assert smoke_set_problems(smoke, docs, markets) == []
    assert smoke.out_of_scope


MODEL_DIR = Path(os.environ.get("E5_MODEL_DIR", ".models/e5"))


def test_the_smoke_set_passes_on_the_real_model() -> None:
    pytest.importorskip("onnxruntime")
    if not (MODEL_DIR / "model.onnx").exists():
        pytest.skip(f"e5 files not in {MODEL_DIR}; fetch them with python -m adapters.shared.e5")
    from adapters.shared.e5 import E5Embedder

    embedder = E5Embedder(MODEL_DIR)
    markets = load_markets(KNOWLEDGE / "markets.yaml")
    docs = load_docs(KNOWLEDGE / "docs")
    store = MemoryChunkStore()
    index(docs, embedder, store)
    smoke = load_smoke_set(KNOWLEDGE / "smoke.yaml")
    knowledge = MemoryKnowledgeStore(store.rows.values())
    report = check_index(docs, markets, embedder, store, knowledge, smoke)
    assert report.passed, report.failures
