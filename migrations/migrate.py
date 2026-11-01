"""Applies migrations/*.sql in order, once each, and records them in schema_migrations.

Each file runs in its own transaction, so a failed migration leaves nothing half-applied. An
applied file must never change: its checksum is recorded, and an edited file stops the run. Fix
forward with a new migration instead.

Runs with the Neon owner connection (ADR 0006), read from Secret Manager into the environment so
it never lands in shell history:

    MIGRATIONS_DATABASE_URL="$(gcloud secrets versions access latest --secret neon-owner-url \\
        --project "$GCP_PROJECT_ID")" uv run python migrations/migrate.py [--status]
"""

import hashlib
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).parent
LOCK_ID = 20_261_101  # pg_advisory_xact_lock key: two runs can't overlap


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    url = os.environ.get("MIGRATIONS_DATABASE_URL")
    if not url:
        print("Set MIGRATIONS_DATABASE_URL (see the docstring).", file=sys.stderr)
        return 2
    status_only = "--status" in sys.argv[1:]
    files = sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))

    with psycopg.connect(url, connect_timeout=30, autocommit=True) as conn:
        tracked = conn.execute("select to_regclass('schema_migrations')").fetchone()
        if tracked is None or tracked[0] is None:
            if status_only:  # read-only: report everything as pending, create nothing
                for path in files:
                    print(f"pending  {path.name}")
                return 0
            conn.execute(
                "create table schema_migrations ("
                " name text primary key, checksum text not null,"
                " applied_at timestamptz not null default now())"
            )
        applied = dict(conn.execute("select name, checksum from schema_migrations").fetchall())

        for path in files:
            if path.name in applied:
                if applied[path.name] != checksum(path):
                    print(f"{path.name} changed after it was applied. Fix forward with a new file.")
                    return 1
                print(f"applied  {path.name}")
                continue
            if status_only:
                print(f"pending  {path.name}")
                continue
            with conn.transaction():
                conn.execute("select pg_advisory_xact_lock(%s)", (LOCK_ID,))
                already = conn.execute(
                    "select 1 from schema_migrations where name = %s", (path.name,)
                ).fetchone()
                if already is None:
                    conn.execute(path.read_text())  # a reviewed file from this repo
                    conn.execute(
                        "insert into schema_migrations (name, checksum) values (%s, %s)",
                        (path.name, checksum(path)),
                    )
            print(f"applied  {path.name} (now)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
