"""Tools the agent can call, and the access rule every tool provider enforces.

A tool's kind sets its tier, so the rule can't be misconfigured per tool:
- read-only tools (e.g. card status) need a signed-in caller
- action tools (e.g. block card) need step-up and the user's explicit confirmation
- anonymous callers get no tools at all: policy answers only

Tools receive the caller's `sub` to check ownership, never a token. Results are masked data only
(e.g. "card ending 4417"); audit records for actions arrive with the first action tool in M3.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.identity import Caller, Tier, tier_allows


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str  # shown to the model, so it must not contain customer data
    parameters: Mapping[str, Any] = field(default_factory=dict)  # JSON Schema for the arguments
    action: bool = False  # changes something for the customer

    @property
    def tier(self) -> Tier:
        return Tier.STEP_UP if self.action else Tier.SIGNED_IN


@dataclass(frozen=True)
class ToolResult:
    content: Mapping[str, Any]  # masked data only


@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked for. Its arguments come from the model: untrusted input."""

    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ToolRequest:
    """The model's turn asked for tools: the last thing an `LLMProvider` stream yields, if any.

    `turn` is the provider's own record of the model's turn (for Gemini, its parts with their
    thought signatures). The core never reads it: it passes it back unchanged in a `ToolStep`, so
    the provider gets its turn back exactly as it sent it (ADR 0008).
    """

    calls: tuple[ToolCall, ...]
    turn: object


@dataclass(frozen=True)
class ToolStep:
    """One step of the tool loop: what the model asked for, and what came back.

    `results` has one entry per call, in the same order: a tool's masked result, or
    `{"error": reason}` for a call that didn't run. They go back to the model as function
    responses, never into the system prompt.
    """

    request: ToolRequest
    results: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if len(self.results) != len(self.request.calls):
            raise ValueError("A tool step needs exactly one result per call")


class ToolError(Exception):
    """A tool call that did not run. `reason` is a short code, safe to log and to show the model."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnknownTool(ToolError):
    def __init__(self) -> None:
        super().__init__("unknown_tool")


class ToolDenied(ToolError):
    pass


def visible_to(spec: ToolSpec, caller: Caller) -> bool:
    return caller.tier is not Tier.ANONYMOUS and tier_allows(caller.tier, spec.tier)


def check_allowed(spec: ToolSpec, caller: Caller, confirmed: bool) -> None:
    """Raises ToolDenied unless this caller may run this tool now."""
    if not visible_to(spec, caller):
        raise ToolDenied(
            "step_up_required" if caller.tier is Tier.SIGNED_IN else "sign_in_required"
        )
    if spec.action and not confirmed:
        raise ToolDenied("confirmation_required")
