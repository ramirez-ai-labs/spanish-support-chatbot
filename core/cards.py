"""The customer's cards and the read-only `check_card_status` tool (ADR 0008).

Cards are synthetic and stored by their last 4 digits only (migration 0005), so a full number
can't reach the model or the page. Ownership is the caller's verified `sub`: the store returns only
that owner's cards, so another customer's card is simply not found, and nothing says it exists.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from core.identity import Caller
from core.tools import ToolDenied, ToolSpec

if TYPE_CHECKING:  # core.ports imports core.tools; this module only needs the type
    from core.ports import CardStore


@dataclass(frozen=True)
class Card:
    last4: str
    kind: str  # "debit" or "credit"
    status: str  # "active" or "blocked"
    expires_on: date  # the last day it can be used
    market: str


CHECK_CARD_STATUS = ToolSpec(
    name="check_card_status",
    # Shown to the model: a fixed string, never customer data.
    description=(
        "Consulta el estado de las tarjetas del cliente que ha iniciado sesión: si están activas "
        "o bloqueadas, y si están vencidas. Úsala cuando el cliente pregunte por una de sus "
        "tarjetas. Con last4, solo esa tarjeta; sin last4, todas las suyas."
    ),
    parameters={
        "type": "object",
        "properties": {
            "last4": {
                "type": "string",
                "description": "Los últimos 4 dígitos de la tarjeta, si el cliente los dio.",
            }
        },
    },
)

LAST4 = re.compile(r"[0-9]{4}")


def masked(card: Card, today: date) -> dict[str, Any]:
    """What the model may see of a card: never more than its last 4 digits."""
    return {
        "kind": card.kind,
        "ending": card.last4,
        "status": card.status,
        "expired": card.expires_on < today,
        "expires": card.expires_on.strftime("%m/%Y"),
    }


def check_card_status(
    store: "CardStore", today: Callable[[], date] = date.today
) -> Callable[[Caller, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]:
    """The tool's handler, reading `store` for the caller's own cards only."""

    async def handler(caller: Caller, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if caller.sub is None:  # check_allowed already refuses anonymous callers
            raise ToolDenied("sign_in_required")
        last4 = arguments.get("last4")
        if last4 is not None and not (isinstance(last4, str) and LAST4.fullmatch(last4)):
            return {"error": "invalid_last4"}  # the model's argument, not trusted
        cards = await asyncio.to_thread(store.cards_of, caller.sub)  # the store call blocks
        if last4 is not None:
            cards = [card for card in cards if card.last4 == last4]
            if not cards:
                return {"error": "card_not_found"}
        now = today()
        return {"cards": [masked(card, now) for card in cards]}

    return handler
