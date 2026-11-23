"""The e5 files are pinned once, in the adapter; the image and CI download through its fetch."""

import hashlib
import io
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from adapters.shared import e5

DOCKERFILE = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()


def test_the_dockerfile_downloads_through_the_adapter() -> None:
    assert "e5.py fetch /layers/models/app/models/e5" in DOCKERFILE
    assert "huggingface.co" not in DOCKERFILE  # no second copy of the pins
    for _name, (_path, sha256) in e5.MODEL_FILES.items():
        assert sha256 not in DOCKERFILE


def test_the_model_id_names_the_pinned_revision() -> None:
    assert e5.REVISION[:7] in e5.MODEL_ID


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _serve(monkeypatch: pytest.MonkeyPatch, outcomes: list[bytes | Exception]) -> list[str]:
    """Each urlopen call takes the next outcome: bytes are the body, an exception is raised."""
    calls: list[str] = []

    def urlopen(url: str, timeout: float) -> _Response:
        calls.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)

    monkeypatch.setattr(e5.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(e5.time, "sleep", lambda _: None)
    return calls


def _pin(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    files = {"model.onnx": ("onnx/model_quantized.onnx", hashlib.sha256(body).hexdigest())}
    monkeypatch.setattr(e5, "MODEL_FILES", files)


def _bad_gateway() -> urllib.error.HTTPError:
    headers: Any = {}
    return urllib.error.HTTPError("https://cdn.example", 502, "Bad Gateway", headers, None)


def test_fetch_retries_the_cdns_502s_and_empty_bodies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _pin(monkeypatch, b"model")
    calls = _serve(monkeypatch, [_bad_gateway(), b"", b"model"])

    e5.fetch(tmp_path)

    assert len(calls) == 3
    assert (tmp_path / "model.onnx").read_bytes() == b"model"


def test_fetch_never_keeps_a_file_whose_checksum_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _pin(monkeypatch, b"model")
    _serve(monkeypatch, [b"tampered"] * e5.FETCH_ATTEMPTS)

    with pytest.raises(RuntimeError, match="checksum mismatch .* refusing the download"):
        e5.fetch(tmp_path)

    assert not (tmp_path / "model.onnx").exists()
