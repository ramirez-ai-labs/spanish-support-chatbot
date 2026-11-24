"""Migration 0005's bank schema and the seed script, against the real Postgres from conftest.py.

Each case runs as the role it's about, so the grants are what's tested: the API's role reads cards
and changes nothing, and no other app role sees the schema. The card tools' contract suite joins
this with the code that reads the cards. Without TEST_DATABASE_URL, these cases skip.
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from migrations.seed_bank import last4s
from tests.contract.conftest import as_role

ROOT = Path(__file__).resolve().parents[2]
SUB = "test-sub-alice"  # synthetic; a real sub is an Identity Platform user ID


@pytest.fixture
def bank(database: str) -> Iterator[str]:
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute("truncate bank.cards")
    yield database


def run_seed(database: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "migrations.seed_bank", *args],
        cwd=ROOT,
        env={**os.environ, "MIGRATIONS_DATABASE_URL": database},
        capture_output=True,
        text=True,
    )


def test_the_seed_gives_a_user_a_debit_and_a_credit_card(bank: str) -> None:
    result = run_seed(bank, "--market", "US", SUB)

    assert result.returncode == 0, result.stderr
    with psycopg.connect(as_role(bank, "app_gcp")) as conn:
        cards = conn.execute(
            "select kind, last4, status, market, expires_on > now()"
            " from bank.cards where owner_sub = %s order by kind",
            (SUB,),
        ).fetchall()
    digits = last4s(SUB)
    assert cards == [
        ("credit", digits["credit"], "active", "US", True),
        ("debit", digits["debit"], "active", "US", True),
    ]


def test_seeding_again_adds_nothing(bank: str) -> None:
    run_seed(bank, "--market", "US", SUB)
    again = run_seed(bank, "--market", "US", SUB)

    assert again.returncode == 0, again.stderr
    assert again.stdout.count("already there") == 2
    with psycopg.connect(bank) as conn:
        assert conn.execute("select count(*) from bank.cards").fetchone() == (2,)


def test_the_seed_refuses_a_market_that_isnt_configured(bank: str) -> None:
    result = run_seed(bank, "--market", "ZZ", SUB)

    assert result.returncode == 2
    assert "not a market" in result.stderr


def test_the_seed_never_logs_a_whole_sub(bank: str) -> None:
    result = run_seed(bank, "--market", "MX", SUB)

    assert SUB not in result.stdout + result.stderr


def test_the_api_role_reads_cards_but_cannot_change_them(bank: str) -> None:
    run_seed(bank, "--market", "US", SUB)
    statements = (
        "insert into bank.cards (owner_sub, market, kind, last4, expires_on)"
        " values ('x', 'US', 'debit', '0000', now())",
        "update bank.cards set status = 'blocked'",
        "delete from bank.cards",
    )
    for statement in statements:
        with (
            psycopg.connect(as_role(bank, "app_gcp")) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute(statement)


def test_other_app_roles_cannot_see_the_bank(bank: str) -> None:
    with (
        psycopg.connect(as_role(bank, "indexer")) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("select 1 from bank.cards")


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("last4", "1234567890123456"),  # 16 digits, as a whole card number would be, never fit
        ("last4", "44a7"),
        ("status", "lost"),
        ("kind", "prepaid"),
        ("market", "usa"),
    ],
)
def test_the_table_refuses_values_outside_its_shape(bank: str, column: str, value: str) -> None:
    row = {"owner_sub": SUB, "market": "US", "kind": "debit", "last4": "4417", "status": "active"}
    row[column] = value
    with psycopg.connect(bank) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into bank.cards (owner_sub, market, kind, last4, status, expires_on)"
            " values (%(owner_sub)s, %(market)s, %(kind)s, %(last4)s, %(status)s, now())",
            row,
        )


def test_one_owner_cannot_have_two_cards_with_the_same_last_4(bank: str) -> None:
    insert = (
        "insert into bank.cards (owner_sub, market, kind, last4, expires_on)"
        " values (%s, 'US', %s, '4417', now())"
    )
    with psycopg.connect(bank, autocommit=True) as conn:
        conn.execute(insert, (SUB, "debit"))
        conn.execute(insert, ("test-sub-bob", "debit"))  # another owner may
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert, (SUB, "credit"))


def test_last4s_are_stable_and_distinct_per_user() -> None:
    first = last4s(SUB)

    assert first == last4s(SUB)
    assert first["debit"] != first["credit"]
    assert all(len(d) == 4 and d.isdigit() for d in first.values())
    assert last4s("test-sub-bob") != first
