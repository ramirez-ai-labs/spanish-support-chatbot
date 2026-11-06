"""Limits that keep the project at $0, checked where a change could quietly break them."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRET_MANAGER_FREE_VERSIONS = 6  # per billing account; each secret holds one version at rest


def test_the_gcp_stack_stays_inside_secret_managers_free_tier() -> None:
    terraform = (ROOT / "infra" / "terraform" / "gcp" / "main.tf").read_text(encoding="utf-8")
    secrets = re.findall(r'^resource "google_secret_manager_secret" "(\w+)"', terraform, re.M)
    assert len(secrets) <= SECRET_MANAGER_FREE_VERSIONS, (
        f"{len(secrets)} secrets: {sorted(secrets)}. A new secret must fit in an existing one with "
        "the same reader (ADR 0006)."
    )
