"""A real Postgres with pgvector for the contract suites that need one.

TEST_DATABASE_URL points at a superuser on a throwaway database: CI's service container, or
`docker compose --profile db up -d db` locally. The fixture applies the migrations and lets the app
roles log in. The test database trusts local connections, so no password exists anywhere, and each
adapter runs as its own role, so the grants from the migrations are tested too. Without it, the
cases that need it skip.
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get("TEST_DATABASE_URL", "")


def as_role(url: str, role: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=f"{role}@{host}"))


@pytest.fixture(scope="session")
def database() -> Iterator[str]:
    if not ADMIN_URL:
        pytest.skip("TEST_DATABASE_URL is not set; start one with `docker compose up -d db`")
    subprocess.run(
        [sys.executable, str(ROOT / "migrations" / "migrate.py")],
        env={**os.environ, "MIGRATIONS_DATABASE_URL": ADMIN_URL},
        check=True,
        capture_output=True,
    )
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        for role in ("indexer", "app_gcp"):
            conn.execute(f"alter role {role} with login")  # fixed names, not input
    yield ADMIN_URL
