"""Contract for Embedder: every adapter must pass every test here.

The real e5 model runs when its files are present: CI fetches them (pinned and checksummed), and
locally `uv run python -m adapters.shared.e5 fetch .models/e5` does the same. Without them, or
where onnxruntime has no wheel, the e5 cases skip and the image's own check covers the model.
"""

import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from adapters.shared.hash_embedder import HashEmbedder
from core.ports import Embedder

MODEL_DIR = Path(os.environ.get("E5_MODEL_DIR", ".models/e5"))


def e5() -> Embedder:
    pytest.importorskip("onnxruntime")
    if not (MODEL_DIR / "model.onnx").exists():
        pytest.skip(f"e5 files not in {MODEL_DIR}; fetch them with python -m adapters.shared.e5")
    from adapters.shared.e5 import E5Embedder

    return E5Embedder(MODEL_DIR)


ADAPTERS: list[Any] = [pytest.param(HashEmbedder, id="hash"), pytest.param(e5, id="e5")]


@pytest.fixture(params=ADAPTERS, scope="module")
def embedder(request: pytest.FixtureRequest) -> Embedder:
    build: Callable[[], Embedder] = request.param
    return build()


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_vectors_have_the_declared_size_and_unit_length(embedder: Embedder) -> None:
    vectors = [embedder.embed_query("¿Cuánto cuesta la tarjeta?")]
    vectors += embedder.embed_passages(["Comisiones de la cuenta", "Bloquear tu tarjeta"])
    for vector in vectors:
        assert len(vector) == embedder.dimensions
        assert math.isclose(math.sqrt(dot(vector, vector)), 1.0, rel_tol=1e-4)


def test_the_same_text_always_gives_the_same_vector(embedder: Embedder) -> None:
    first = embedder.embed_query("¿Cómo deposito un cheque?")
    second = embedder.embed_query("¿Cómo deposito un cheque?")
    assert dot(first, second) == pytest.approx(1.0, abs=1e-6)


def test_a_batch_matches_one_at_a_time_in_order(embedder: Embedder) -> None:
    texts = ["Remesas desde Estados Unidos", "Comisión por manejo de cuenta", "Tu IBAN"]
    batch = embedder.embed_passages(texts)
    assert len(batch) == len(texts)
    for text, vector in zip(texts, batch, strict=True):
        assert dot(vector, embedder.embed_passages([text])[0]) == pytest.approx(1.0, abs=1e-4)
    assert embedder.embed_passages([]) == []


def test_a_related_passage_ranks_above_an_unrelated_one(embedder: Embedder) -> None:
    query = embedder.embed_query("Me robaron la tarjeta, ¿cómo la bloqueo?")
    related, unrelated = embedder.embed_passages(
        [
            "Bloquear tu tarjeta: en la app, entra a Tarjetas y elige Bloquear tarjeta.",
            "Remesas: puedes recibir dinero desde Estados Unidos directamente en tu cuenta.",
        ]
    )
    assert dot(query, related) > dot(query, unrelated)


def test_the_model_name_is_recorded_with_its_version(embedder: Embedder) -> None:
    assert embedder.model and embedder.model.strip() == embedder.model
