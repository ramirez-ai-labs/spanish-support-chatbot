"""The helper that reads and updates merged Neon secrets (ADR 0006). All values are synthetic."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "neon_secret_json.py"
URL = "postgresql://role:synthetic-password@db.example.invalid/chatbot"


def run(
    *args: str, stdin: str = "{}", new_value: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "NEW_VALUE"}
    if new_value is not None:
        env["NEW_VALUE"] = new_value
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_set_adds_a_key_and_keeps_the_others() -> None:
    done = run("set", "dev", stdin='{"main": "other"}', new_value=URL)
    assert done.returncode == 0
    assert json.loads(done.stdout) == {"main": "other", "dev": URL}


def test_set_replaces_a_key() -> None:
    done = run("set", "dev", stdin='{"dev": "old"}', new_value=URL)
    assert json.loads(done.stdout) == {"dev": URL}


def test_set_refuses_an_empty_value() -> None:
    for value in (None, ""):
        done = run("set", "dev", stdin='{"dev": "old"}', new_value=value)
        assert done.returncode == 1
        assert done.stdout == ""


def test_get_prints_only_the_value() -> None:
    done = run("get", "main", stdin=json.dumps({"main": URL, "dev": "other"}))
    assert done.returncode == 0
    assert done.stdout == URL


def test_get_fails_on_a_missing_key() -> None:
    done = run("get", "dev", stdin=json.dumps({"main": URL}))
    assert done.returncode == 1
    assert done.stdout == ""


def test_has_answers_by_exit_code_and_prints_nothing() -> None:
    stdin = json.dumps({"main": URL, "empty": ""})
    assert run("has", "main", stdin=stdin).returncode == 0
    assert run("has", "dev", stdin=stdin).returncode == 1
    assert run("has", "empty", stdin=stdin).returncode == 1
    assert run("has", "main", stdin=stdin).stdout == ""


@pytest.mark.parametrize("stdin", [URL, "", "[1, 2]"])
def test_a_value_that_is_not_a_json_object_is_refused_without_echoing_it(stdin: str) -> None:
    done = run("get", "main", stdin=stdin)
    assert done.returncode == 1
    assert done.stdout == ""
    assert "synthetic-password" not in done.stderr


def test_unknown_commands_are_refused() -> None:
    assert run("delete", "main").returncode == 2
    assert run("get").returncode == 2
