"""Gives test users synthetic cards in `bank.cards`, for the customer tools (ADR 0008).

Synthetic data only: a debit and a credit card per user, active, with last 4 digits derived from
the user's sub, so a rerun finds the same cards and adds nothing. Only the last 4 digits exist.

A test user's sub is their Identity Platform user ID (the token's `sub`), which exists once they
have signed in by email link. Subs are passed as arguments and never committed. Runs with the
owner connection, like migrate.py, after 0005 is applied, from the repo root:

    MIGRATIONS_DATABASE_URL="$(gcloud secrets versions access latest --secret neon-owner-urls \\
        --project "$GCP_PROJECT_ID" | python3 infra/scripts/neon_secret_json.py get main)" \\
        uv run python -m migrations.seed_bank --market US <sub> [<sub> ...]
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg

from core.knowledge import load_markets

MARKETS = Path(__file__).resolve().parents[1] / "knowledge" / "markets.yaml"
# Valid for about 3 and 4 years from the seed, to the month's last day.
CARDS = (("debit", 37), ("credit", 49))


def last4s(sub: str) -> dict[str, str]:
    """The same 4 digits for the same sub on every run, and never the same for a sub's two cards."""
    digits: dict[str, str] = {}
    for kind, _months in CARDS:
        n = int.from_bytes(hashlib.sha256(f"{sub}:{kind}".encode()).digest()[:4]) % 10_000
        while f"{n:04d}" in digits.values():
            n = (n + 1) % 10_000
        digits[kind] = f"{n:04d}"
    return digits


def seed(conn: psycopg.Connection[tuple[object, ...]], sub: str, market: str) -> list[str]:
    """Adds the sub's cards that aren't there yet; returns a line per card for the log."""
    lines = []
    for kind, months in CARDS:
        last4 = last4s(sub)[kind]
        added = conn.execute(
            "insert into bank.cards (owner_sub, market, kind, last4, expires_on)"
            " values (%s, %s, %s, %s,"
            " (date_trunc('month', now()) + make_interval(months => %s) - interval '1 day')::date)"
            " on conflict (owner_sub, last4) do nothing",
            (sub, market, kind, last4, months),
        ).rowcount
        lines.append(f"{kind} ending {last4}: {'added' if added else 'already there'}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--market", required=True, help="a code from knowledge/markets.yaml")
    parser.add_argument("subs", nargs="+", metavar="sub", help="a test user's Identity Platform ID")
    args = parser.parse_args()
    url = os.environ.get("MIGRATIONS_DATABASE_URL")
    if not url:
        print("Set MIGRATIONS_DATABASE_URL (see the docstring).", file=sys.stderr)
        return 2
    if args.market not in load_markets(MARKETS):
        print(f"{args.market}: not a market in knowledge/markets.yaml", file=sys.stderr)
        return 2
    with psycopg.connect(url, connect_timeout=30) as conn:  # one transaction: all users or none
        for sub in args.subs:
            for line in seed(conn, sub, args.market):
                print(f"{sub[:4]}…: {line}")  # the start of the sub is enough to tell users apart
    return 0


if __name__ == "__main__":
    sys.exit(main())
