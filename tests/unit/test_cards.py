"""check_card_status (ADR 0008): the caller's own cards, masked, whatever the model asks for."""

import asyncio
from collections.abc import Mapping
from datetime import date
from typing import Any

import pytest

from adapters.shared.memory_cards import MemoryCardStore
from core.cards import CHECK_CARD_STATUS, Card, check_card_status
from core.identity import ANONYMOUS, Caller, Tier
from core.tools import ToolDenied

ALICE = Caller(Tier.SIGNED_IN, sub="test-sub-alice")
BOB = Caller(Tier.SIGNED_IN, sub="test-sub-bob")
STORE = MemoryCardStore(
    [
        ("test-sub-alice", Card("4417", "debit", "active", date(2029, 12, 31), "US")),
        ("test-sub-alice", Card("0280", "credit", "blocked", date(2026, 10, 31), "US")),
        ("test-sub-bob", Card("9999", "debit", "active", date(2029, 12, 31), "MX")),
    ]
)
TODAY = date(2026, 11, 24)


def run(caller: Caller, **arguments: Any) -> Mapping[str, Any]:
    return asyncio.run(check_card_status(STORE, today=lambda: TODAY)(caller, arguments))


def test_without_last4_it_lists_all_the_callers_cards() -> None:
    assert run(ALICE) == {
        "cards": [
            {
                "kind": "credit",
                "ending": "0280",
                "status": "blocked",
                "expired": True,
                "expires": "10/2026",
            },
            {
                "kind": "debit",
                "ending": "4417",
                "status": "active",
                "expired": False,
                "expires": "12/2029",
            },
        ]
    }


def test_with_last4_it_returns_only_that_card() -> None:
    result = run(ALICE, last4="4417")

    assert [card["ending"] for card in result["cards"]] == ["4417"]


def test_another_customers_card_is_simply_not_found() -> None:
    # Bob's card exists, but only the caller's own cards are ever read: nothing says it exists.
    assert run(ALICE, last4="9999") == {"error": "card_not_found"}
    assert run(BOB, last4="9999")["cards"][0]["ending"] == "9999"


@pytest.mark.parametrize("last4", ["44", "44170", "abcd", "4417 ", 4417, None])
def test_a_malformed_last4_from_the_model_is_refused(last4: object) -> None:
    result = asyncio.run(check_card_status(STORE)(ALICE, {"last4": last4}))

    if last4 is None:  # absent and null are the same: all cards
        assert "cards" in result
    else:
        assert result == {"error": "invalid_last4"}


def test_an_owner_argument_from_the_model_is_ignored() -> None:
    # The owner is always the verified caller; the model has no way to name another one.
    result = asyncio.run(check_card_status(STORE)(ALICE, {"sub": "test-sub-bob"}))

    assert {card["ending"] for card in result["cards"]} == {"4417", "0280"}


def test_an_anonymous_caller_is_refused_even_if_called_directly() -> None:
    with pytest.raises(ToolDenied):
        run(ANONYMOUS)


def test_the_tool_is_read_only_and_needs_a_signed_in_caller() -> None:
    assert not CHECK_CARD_STATUS.action
    assert CHECK_CARD_STATUS.tier is Tier.SIGNED_IN
