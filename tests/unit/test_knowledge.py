"""The knowledge base in `knowledge/`: every doc parses, sits in its market's folder, covers the
same topics as the other markets, and contains nothing shaped like real personal data."""

import re
from collections import Counter
from pathlib import Path

import pytest

from core.knowledge import ALL, Doc, load_docs, load_markets, parse_doc

ROOT = Path(__file__).resolve().parents[2] / "knowledge"
MARKETS = load_markets(ROOT / "markets.yaml")
DOCS = load_docs(ROOT / "docs")

# Shapes of real identifiers. Synthetic docs never need one (CLAUDE.md: synthetic data only).
REAL_DATA_SHAPES = {
    "card number": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "CURP": re.compile(r"\b[A-Z]{4}\d{6}[HM][A-Z]{5}[A-Z0-9]\d\b"),
    "DUI": re.compile(r"\b\d{8}-\d\b"),
    "DNI or NIE": re.compile(r"\b[XYZ]?\d{7,8}[A-Z]\b"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "phone number": re.compile(r"\+?\d[\d ()-]{8,}\d"),
}


def test_markets_are_the_four_from_adr_0007() -> None:
    assert set(MARKETS) == {"US", "MX", "SV", "ES"}
    assert {m.currency for m in MARKETS.values()} == {"USD", "MXN", "EUR"}


def test_corpus_size_is_within_the_roadmap_range() -> None:
    assert 20 <= len(DOCS) <= 40


def test_every_doc_belongs_to_a_known_market() -> None:
    assert {doc.market for doc in DOCS} <= set(MARKETS) | {ALL}


def test_doc_ids_are_unique() -> None:
    assert [i for i, n in Counter(d.doc_id for d in DOCS).items() if n > 1] == []


def test_every_market_covers_the_same_topics() -> None:
    # The demo compares markets on the same topic, so no market may be missing one.
    topics = {doc.topic for doc in DOCS if doc.market != ALL}
    for market in MARKETS:
        covered = {doc.topic for doc in DOCS if doc.market == market}
        assert covered == topics, f"{market} is missing {sorted(topics - covered)}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d.doc_id)
def test_doc_is_sections_under_level_two_headings(doc: Doc) -> None:
    assert doc.title.strip()
    assert doc.body.startswith("## "), "start with a ## section: the indexer chunks by section"
    assert not re.search(r"^# ", doc.body, flags=re.M), "the title lives in the front matter"
    sections = re.findall(r"^## (.+)$", doc.body, flags=re.M)
    assert len(sections) == len(set(sections)), "section headings repeat"


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d.doc_id)
def test_doc_contains_no_real_data_shapes(doc: Doc) -> None:
    text = f"{doc.title}\n{doc.body}"
    found = {name for name, shape in REAL_DATA_SHAPES.items() if shape.search(text)}
    assert found == set()


def test_the_data_check_catches_real_shapes() -> None:
    samples = ["4111 1111 1111 1111", "123-45-6789", "GODE561231HDFRRN09", "01234567-8"]
    samples += ["12345678Z", "ana@example.com", "+52 55 1234 5678"]
    for sample in samples:
        assert any(shape.search(sample) for shape in REAL_DATA_SHAPES.values()), sample


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("## Sin front matter\n", "missing front matter"),
        ("---\ndoc_id: x\n", "not closed"),
        ("---\ndoc_id: x\nmarket: MX\n---\n## A\n", "keys must be exactly"),
        (
            "---\ndoc_id: x\nmarket: MX\ntitle: t\ntopic: t\neffective_date: pronto\n---\n## A\n",
            "must be a date",
        ),
    ],
)
def test_malformed_docs_are_refused_with_their_source(text: str, problem: str) -> None:
    with pytest.raises(ValueError, match=problem) as raised:
        parse_doc(text, "docs/MX/x.md")
    assert "docs/MX/x.md" in str(raised.value)


def test_a_doc_in_the_wrong_folder_is_refused(tmp_path: Path) -> None:
    (tmp_path / "ES").mkdir()
    (tmp_path / "ES" / "mx-x.md").write_text(
        "---\ndoc_id: mx-x\nmarket: MX\ntitle: t\ntopic: t\neffective_date: 2026-10-01\n---\n## A\n"
    )
    with pytest.raises(ValueError, match="must be at MX/mx-x.md"):
        load_docs(tmp_path)
