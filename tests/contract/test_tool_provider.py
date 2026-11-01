"""Contract for ToolProvider: every adapter must pass every test here.

Each adapter is built with the same two test tools: a read-only one and an action. To add an adapter
(the MCP client in M9), add one entry to ADAPTERS that serves these tools through it.
"""

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from adapters.shared.in_process_tools import InProcessToolProvider, Tool
from core.identity import ANONYMOUS, Caller, Tier
from core.ports import ToolProvider
from core.tools import ToolDenied, ToolSpec, UnknownTool

SIGNED_IN = Caller(Tier.SIGNED_IN, sub="user-123")
STEP_UP = Caller(Tier.STEP_UP, sub="user-123")

CARD_STATUS = ToolSpec(
    "card_status",
    "Estado de la tarjeta del cliente.",
    {"type": "object", "properties": {}},
)
BLOCK_CARD = ToolSpec(
    "block_card",
    "Bloquea la tarjeta del cliente.",
    {"type": "object", "properties": {"reason": {"type": "string"}}},
    action=True,
)


async def card_status(caller: Caller, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"owner": caller.sub, "card": "ending 4417", "status": "active"}


async def block_card(caller: Caller, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"owner": caller.sub, "card": "ending 4417", "status": "blocked", **arguments}


def in_process() -> ToolProvider:
    return InProcessToolProvider([Tool(CARD_STATUS, card_status), Tool(BLOCK_CARD, block_card)])


ADAPTERS: list[Any] = [pytest.param(in_process, id="in_process")]


@pytest.fixture(params=ADAPTERS)
def tools(request: pytest.FixtureRequest) -> ToolProvider:
    build: Callable[[], ToolProvider] = request.param
    return build()


def call(tools: ToolProvider, caller: Caller, name: str, confirmed: bool = False) -> Any:
    return asyncio.run(tools.call(caller, name, {"reason": "lost"}, confirmed=confirmed))


def names(tools: ToolProvider, caller: Caller) -> set[str]:
    return {spec.name for spec in tools.list_tools(caller)}


def test_each_tier_sees_only_its_tools(tools: ToolProvider) -> None:
    assert names(tools, ANONYMOUS) == set()
    assert names(tools, SIGNED_IN) == {"card_status"}
    assert names(tools, STEP_UP) == {"card_status", "block_card"}


def test_listed_tools_carry_what_the_model_needs(tools: ToolProvider) -> None:
    (spec,) = tools.list_tools(SIGNED_IN)
    assert spec.description and spec.parameters["type"] == "object"


def test_anonymous_callers_cannot_call_any_tool(tools: ToolProvider) -> None:
    for name in ("card_status", "block_card"):
        with pytest.raises(ToolDenied) as denied:
            call(tools, ANONYMOUS, name, confirmed=True)
        assert denied.value.reason == "sign_in_required"


def test_read_only_tool_runs_for_a_signed_in_caller_with_their_sub(tools: ToolProvider) -> None:
    result = call(tools, SIGNED_IN, "card_status")
    assert result.content["owner"] == "user-123"  # the tool can check ownership
    assert result.content["card"] == "ending 4417"  # masked


def test_action_needs_step_up(tools: ToolProvider) -> None:
    with pytest.raises(ToolDenied) as denied:
        call(tools, SIGNED_IN, "block_card", confirmed=True)
    assert denied.value.reason == "step_up_required"


def test_action_needs_explicit_confirmation(tools: ToolProvider) -> None:
    with pytest.raises(ToolDenied) as denied:
        call(tools, STEP_UP, "block_card")
    assert denied.value.reason == "confirmation_required"


def test_confirmed_action_runs_after_step_up(tools: ToolProvider) -> None:
    result = call(tools, STEP_UP, "block_card", confirmed=True)
    assert result.content["status"] == "blocked"
    assert result.content["reason"] == "lost"


def test_unknown_tool_is_refused(tools: ToolProvider) -> None:
    with pytest.raises(UnknownTool) as refused:
        call(tools, STEP_UP, "transfer_money", confirmed=True)
    assert refused.value.reason == "unknown_tool"
