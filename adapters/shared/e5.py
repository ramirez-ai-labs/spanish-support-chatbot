"""Embedder: multilingual-e5-small, int8 ONNX, run in the container (ADR 0007, #50).

The files are pinned by revision and SHA-256 (MODEL_FILES) and baked into the image, so instances
never download them. Inference is onnxruntime plus the Rust tokenizer, with no PyTorch: mean
pooling over the attention mask, then L2 normalization, as the model card specifies. Indexing and
queries use this same adapter, so their vectors always match.

    python -m adapters.shared.e5 fetch <dir>   # download and verify the pinned files
    python -m adapters.shared.e5 check <dir>   # load them and run a Spanish retrieval check
"""

import hashlib
import sys
import time
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

REPOSITORY = (
    "Xenova/multilingual-e5-small"  # an ONNX export of intfloat/multilingual-e5-small (MIT)
)
REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
# Local name -> (path in the repository, SHA-256). The Dockerfile pins the same values; a test
# checks they match.
MODEL_FILES = {
    "model.onnx": (
        "onnx/model_quantized.onnx",
        "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193",
    ),
    "tokenizer.json": (
        "tokenizer.json",
        "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    ),
}
MODEL_ID = f"{REPOSITORY}@{REVISION[:7]}:int8"  # recorded with every chunk
DIMENSIONS = 384
MAX_TOKENS = 512  # longer text is truncated; chunks are kept well below this


def file_url(path: str) -> str:
    return f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{path}"


class E5Embedder:
    model = MODEL_ID
    dimensions = DIMENSIONS

    def __init__(self, model_dir: Path, threads: int = 1) -> None:
        # Imported here: onnxruntime has no wheel for every developer machine (Intel Macs on
        # macOS 12), and only this adapter needs it.
        import onnxruntime as ort
        from tokenizers import Tokenizer

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads  # Cloud Run gives one vCPU
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_dir / "model.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self._inputs = {i.name for i in self._session.get_inputs()}
        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self._tokenizer.enable_truncation(MAX_TOKENS)
        pad = self._tokenizer.token_to_id("<pad>")
        self._tokenizer.enable_padding(pad_id=pad if pad is not None else 1, pad_token="<pad>")

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        # One at a time: the int8 model quantizes a batch as a whole, so padding in a batch
        # changes a text's vector slightly (cosine 0.998). A text must always get the same one,
        # and at ~70 ms a text the whole corpus still embeds in seconds.
        return [self._embed([f"passage: {text}"])[0] for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed([f"query: {text}"])[0]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        encoded = self._tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in encoded], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        feeds: dict[str, Any] = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = self._session.run(None, feeds)[0]  # last_hidden_state: (batch, tokens, 384)
        weights = mask[..., None].astype(hidden.dtype)
        pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1, None)
        pooled /= np.linalg.norm(pooled, axis=1, keepdims=True)
        vectors: list[list[float]] = pooled.astype(float).tolist()
        return vectors


def fetch(target: Path) -> None:
    """Downloads the pinned files into `target`, refusing any whose checksum doesn't match."""
    target.mkdir(parents=True, exist_ok=True)
    for name, (path, sha256) in MODEL_FILES.items():
        destination = target / name
        if destination.exists() and _sha256(destination) == sha256:
            continue
        with urllib.request.urlopen(file_url(path), timeout=120) as response:  # noqa: S310 (fixed https URL)
            data = response.read()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise RuntimeError(f"{name}: checksum mismatch; refusing the download")
        destination.write_bytes(data)


def check(model_dir: Path) -> None:
    """Loads the model and checks it ranks a Spanish policy passage above an unrelated one."""
    started = time.perf_counter()
    embedder = E5Embedder(model_dir)
    loaded = time.perf_counter()
    query = embedder.embed_query("¿Cómo bloqueo mi tarjeta si me la robaron?")
    related, unrelated = embedder.embed_passages(
        [
            "Bloquear tu tarjeta: en la app, entra a Tarjetas y elige Bloquear tarjeta.",
            "Remesas: puedes recibir dinero desde Estados Unidos directamente en tu cuenta.",
        ]
    )
    scores = [float(np.dot(query, related)), float(np.dot(query, unrelated))]
    print(
        f"{MODEL_ID}: loaded in {loaded - started:.2f} s, query in"
        f" {(time.perf_counter() - loaded) * 1000 / 3:.0f} ms per text;"
        f" related {scores[0]:.3f} > unrelated {scores[1]:.3f}"
    )
    if len(query) != DIMENSIONS or not scores[0] > scores[1]:
        raise SystemExit("e5 check failed")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    command, directory = sys.argv[1], Path(sys.argv[2])
    {"fetch": fetch, "check": check}[command](directory)
