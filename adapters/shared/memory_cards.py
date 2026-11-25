"""CardStore in memory, for tests and local runs: the same contract as Neon's bank.cards."""

from collections.abc import Iterable

from core.cards import Card


class MemoryCardStore:
    def __init__(self, cards: Iterable[tuple[str, Card]] = ()) -> None:
        """`cards` are (owner sub, card) pairs."""
        self._cards: list[tuple[str, Card]] = list(cards)

    def cards_of(self, sub: str) -> list[Card]:
        mine = [card for owner, card in self._cards if owner == sub]
        return sorted(mine, key=lambda card: (card.kind, card.last4))
