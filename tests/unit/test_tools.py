import pytest

from adapters.shared.in_process_tools import InProcessToolProvider, Tool
from core.identity import Tier, tier_allows
from core.tools import ToolCall, ToolRequest, ToolSpec, ToolStep


def test_tool_kind_sets_its_tier() -> None:
    assert ToolSpec("status", "Estado").tier == Tier.SIGNED_IN
    assert ToolSpec("block", "Bloquea", action=True).tier == Tier.STEP_UP


@pytest.mark.parametrize(
    ("held", "required", "allowed"),
    [
        (Tier.ANONYMOUS, Tier.SIGNED_IN, False),
        (Tier.SIGNED_IN, Tier.SIGNED_IN, True),
        (Tier.SIGNED_IN, Tier.STEP_UP, False),
        (Tier.STEP_UP, Tier.SIGNED_IN, True),
        (Tier.STEP_UP, Tier.STEP_UP, True),
    ],
)
def test_tiers_are_ordered(held: Tier, required: Tier, allowed: bool) -> None:
    assert tier_allows(held, required) is allowed


def test_duplicate_tool_names_are_refused() -> None:
    async def handler(caller: object, arguments: object) -> dict[str, str]:
        return {}

    spec = ToolSpec("status", "Estado")
    with pytest.raises(ValueError, match="Duplicate tool name"):
        InProcessToolProvider([Tool(spec, handler), Tool(spec, handler)])


def test_a_tool_step_needs_one_result_per_call() -> None:
    request = ToolRequest((ToolCall("status", {}), ToolCall("status", {"last4": "4417"})), object())

    ToolStep(request, ({"status": "active"}, {"error": "not_found"}))
    with pytest.raises(ValueError, match="one result per call"):
        ToolStep(request, ({"status": "active"},))
