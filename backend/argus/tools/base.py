"""Tool interface and registry.

Every tool declares risk tags (``shell``, ``network``, ``network_write``,
``fs_write``, ``remote_agent``, ``package_install``...) so the guardrail
engine can demand human approval without knowing the tool's internals.
Tool outputs are untrusted data and are screened before agents see them.
"""

from __future__ import annotations

import abc
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, BeforeValidator, ValidationError

from argus.providers.types import ToolSpec

if TYPE_CHECKING:
    from argus.runtime.context import RunContext

# Default wall-clock limit for one tool call; tools with longer or self-enforced limits override
# ``Tool.timeout_for``.
TOOL_TIMEOUT_S = 300.0


def _coerce_str_list(value: Any) -> Any:
    """Accept the shapes models actually send for a list of strings.

    Models often pass ``"a, b"`` or a JSON-encoded array instead of a real array; rejecting those
    wastes a whole turn, so split them here. Anything else is left for normal validation.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = None
        if isinstance(decoded, list):
            return [str(v).strip() for v in decoded if str(v).strip()]
    return [part.strip() for part in text.split(",") if part.strip()]


# A ``list[str]`` tool argument that also takes a comma-separated or JSON-encoded string.
StrList = Annotated[list[str], BeforeValidator(_coerce_str_list)]


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def error(cls, message: str) -> ToolResult:
        return cls(content=message, is_error=True)


@dataclass
class ToolCallContext:
    run: RunContext
    member_id: str
    member_name: str
    role: str
    step_id: str | None
    tool_call_id: str
    emit_output: Callable[[str, str], Awaitable[None]] | None = None


class Tool(abc.ABC):
    name: str
    description: str
    Args: type[BaseModel]
    risk: frozenset[str] = frozenset()
    source: str = "builtin"  # builtin | mcp | skill | a2a | acp

    def spec(self) -> ToolSpec:
        schema = self.Args.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(name=self.name, description=self.description, input_schema=schema)

    def risk_for(self, args: BaseModel) -> frozenset[str]:
        """Risk tags for a specific invocation; override for argument-dependent risk."""
        return self.risk

    def timeout_for(self, args: BaseModel | None) -> float | None:
        """Wall-clock limit for one call (``args`` is None when they do not validate).

        ``None`` means the tool enforces its own limit and must not be cut off from outside.
        """
        return TOOL_TIMEOUT_S

    async def invoke(self, raw_args: dict[str, Any], ctx: ToolCallContext) -> ToolResult:
        try:
            args = self.Args.model_validate(raw_args)
        except ValidationError as exc:
            errors = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
            return ToolResult.error(f"Invalid arguments for {self.name}: {errors}")
        return await self.run(args, ctx)

    def parse(self, raw_args: dict[str, Any]) -> BaseModel | None:
        try:
            return self.Args.model_validate(raw_args)
        except ValidationError:
            return None

    @abc.abstractmethod
    async def run(self, args: Any, ctx: ToolCallContext) -> ToolResult: ...


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs_for(self, allowed: Callable[[str], bool]) -> list[ToolSpec]:
        return [t.spec() for name, t in sorted(self._tools.items()) if allowed(name)]

    def __contains__(self, name: str) -> bool:
        return name in self._tools
