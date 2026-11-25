"""Contract for CardStore: every adapter must pass every test here.

The Neon adapter reads as `app_gcp` against the real Postgres from conftest.py, so migration 0005's
grants are part of what's tested; without TEST_DATABASE_URL, those cases skip. Synthetic data only.
"""

from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import psycopg
import pytest

from adapters.shared.memory_cards import MemoryCardStore
from adapters.shared.neon_cards import NeonCardStore
from core.cards import Card
from core.ports import CardStore
from tests.contract.conftest import as_role

ALICE, BOB = "test-sub-alice", "test-sub-bob"
CARDS = [
    (ALICE, Card("4417", "debit", "active", date(2029, 12, 31), "US")),
    (ALICE, Card("0280", "credit", "blocked", date(2030, 6, 30), "US")),
    (ALICE, Card("1111", "debit", "active", date(2020, 1, 31), "US")),  # expired
    (BOB, Card("9999", "debit", "active", date(2029, 12, 31), "MX")),
]


@pytest.fixture
def neon(database: str) -> Iterator[CardStore]:
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute("truncate bank.cards")
        for sub, card in CARDS:
            conn.execute(
                "insert into bank.cards (owner_sub, market, kind, last4, status, expires_on)"
                " values (%s, %s, %s, %s, %s, %s)",
                (sub, card.market, card.kind, card.last4, card.status, card.expires_on),
            )
    yield NeonCardStore(as_role(database, "app_gcp"))


ADAPTERS: list[Any] = [
    pytest.param(lambda request: MemoryCardStore(CARDS), id="memory"),
    pytest.param(lambda request: request.getfixturevalue("neon"), id="neon"),
]


@pytest.fixture(params=ADAPTERS)
def store(request: pytest.FixtureRequest) -> CardStore:
    build: Callable[[pytest.FixtureRequest], CardStore] = request.param
    return build(request)


def test_an_owner_gets_only_their_own_cards(store: CardStore) -> None:
    assert {card.last4 for card in store.cards_of(ALICE)} == {"4417", "0280", "1111"}
    assert [card.last4 for card in store.cards_of(BOB)] == ["9999"]


def test_an_owner_with_no_cards_gets_none(store: CardStore) -> None:
    assert store.cards_of("test-sub-nobody") == []


def test_cards_come_by_kind_then_last_4(store: CardStore) -> None:
    assert [(c.kind, c.last4) for c in store.cards_of(ALICE)] == [
        ("credit", "0280"),
        ("debit", "1111"),
        ("debit", "4417"),
    ]


def test_a_card_carries_what_the_tool_shows(store: CardStore) -> None:
    (blocked,) = [card for card in store.cards_of(ALICE) if card.last4 == "0280"]

    assert blocked == Card("0280", "credit", "blocked", date(2030, 6, 30), "US")


def test_a_sub_is_matched_exactly_never_as_a_pattern(store: CardStore) -> None:
    # The sub is a bound parameter: wildcards and quotes are just characters.
    assert store.cards_of("test-sub-%") == []
    assert store.cards_of("' or '1'='1") == []
