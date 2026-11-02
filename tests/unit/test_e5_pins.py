"""The e5 files are pinned twice, in the adapter and the Dockerfile; they must agree."""

from pathlib import Path

from adapters.shared import e5

DOCKERFILE = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()


def test_the_dockerfile_bakes_exactly_the_pinned_files() -> None:
    for name, (path, sha256) in e5.MODEL_FILES.items():
        assert f"--checksum=sha256:{sha256}" in DOCKERFILE, name
        assert e5.file_url(path) in DOCKERFILE, name
        assert f"/app/models/e5/{name}" in DOCKERFILE, name
    assert DOCKERFILE.count("--checksum=sha256:") == len(e5.MODEL_FILES)


def test_the_model_id_names_the_pinned_revision() -> None:
    assert e5.REVISION[:7] in e5.MODEL_ID
