import datetime as dt
from pathlib import Path

from core.chunking import MAX_WORDS, chunk_doc
from core.knowledge import Doc, load_docs

KNOWLEDGE = Path(__file__).resolve().parents[2] / "knowledge" / "docs"


def doc(body: str) -> Doc:
    return Doc("mx-x", "MX", "Comisiones (México)", "comisiones", dt.date(2026, 10, 1), body)


def test_one_chunk_per_section_with_its_heading() -> None:
    chunks = chunk_doc(doc("## Cajeros\n\nSin costo.\n\n## Reposición\n\nCuesta $150 MXN."))
    assert [(c.heading_path, c.content) for c in chunks] == [
        (("Cajeros",), "Sin costo."),
        (("Reposición",), "Cuesta $150 MXN."),
    ]
    assert {(c.doc_id, c.market, c.title) for c in chunks} == {
        ("mx-x", "MX", "Comisiones (México)")
    }


def test_subsections_keep_both_headings() -> None:
    chunks = chunk_doc(doc("## Tarjetas\n\nIntro.\n\n### Robo\n\nBloquéala."))
    assert [c.heading_path for c in chunks] == [("Tarjetas",), ("Tarjetas", "Robo")]


def test_the_passage_names_the_doc_and_section() -> None:
    (chunk,) = chunk_doc(doc("## Cajeros\n\nSin costo."))
    assert chunk.passage() == "Comisiones (México) — Cajeros\n\nSin costo."


def test_a_long_section_splits_at_paragraphs_and_repeats_one() -> None:
    paragraphs = [f"Regla {i}. " + "palabra " * 100 for i in range(4)]
    chunks = chunk_doc(doc("## Larga\n\n" + "\n\n".join(paragraphs)))
    assert len(chunks) > 1
    assert all(c.heading_path == ("Larga",) for c in chunks)
    for before, after in zip(chunks, chunks[1:], strict=False):
        assert after.content.split("\n\n")[0] == before.content.split("\n\n")[-1]


def test_the_hash_changes_with_the_text_or_the_model() -> None:
    (a,) = chunk_doc(doc("## Cajeros\n\nSin costo."))
    (b,) = chunk_doc(doc("## Cajeros\n\nCuesta $25 MXN."))
    assert a.content_hash("m1") == a.content_hash("m1")
    assert a.content_hash("m1") != b.content_hash("m1")
    assert a.content_hash("m1") != a.content_hash("m2")


def test_the_real_corpus_chunks_into_short_sections() -> None:
    chunks = [c for d in load_docs(KNOWLEDGE) for c in chunk_doc(d)]
    assert len(chunks) == 3 * 28  # three sections in every doc
    assert all(len(c.content.split()) <= MAX_WORDS for c in chunks)
    assert len({c.content_hash("m") for c in chunks}) == len(chunks)
