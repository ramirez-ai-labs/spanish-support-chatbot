"""ToolProvider that runs tools inside the API process: the customer hot path, no network hop."""

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.identity import Caller
from core.tools import ToolResult, ToolSpec, UnknownTool, check_allowed, visible_to

# A tool's code: gets the verified caller (for ownership checks against `caller.sub`) and the
# model's arguments, and returns masked data.
Handler = Callable[[Caller, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    handler: Handler


class InProcessToolProvider:
    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.spec.name in self._tools:
                raise ValueError(f"Duplicate tool name: {tool.spec.name!r}")
            self._tools[tool.spec.name] = tool

    def list_tools(self, caller: Caller) -> Sequence[ToolSpec]:
        return [t.spec for t in self._tools.values() if visible_to(t.spec, caller)]

    async def call(
        self, caller: Caller, name: str, arguments: Mapping[str, Any], confirmed: bool = False
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            raise UnknownTool()
        check_allowed(tool.spec, caller, confirmed)
        return ToolResult(content=await tool.handler(caller, arguments))
