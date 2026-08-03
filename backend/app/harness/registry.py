from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from app.harness.contracts import HarnessToolContext, HarnessToolSpec

HarnessToolHandler = Callable[[HarnessToolContext, BaseModel], Mapping[str, Any]]
_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class RegisteredHarnessTool:
    spec: HarnessToolSpec
    argument_model: type[BaseModel]
    handler: HarnessToolHandler


class HarnessRegistry:
    """Typed, deterministic registry for capabilities exposed to a Harness run."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredHarnessTool] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        argument_model: type[BaseModel],
        handler: HarnessToolHandler,
        side_effect: Literal["read", "write", "delete"] = "read",
    ) -> HarnessToolSpec:
        if not _TOOL_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid Harness tool name: {name!r}")
        if name in self._tools:
            raise ValueError(f"Harness tool already registered: {name}")
        if not description.strip():
            raise ValueError("Harness tool description cannot be empty")
        if not isinstance(argument_model, type) or not issubclass(argument_model, BaseModel):
            raise TypeError("argument_model must be a Pydantic BaseModel type")
        if side_effect not in {"read", "write", "delete"}:
            raise ValueError("side_effect must be read, write, or delete")

        spec = HarnessToolSpec(
            name=name,
            description=description.strip(),
            input_schema=argument_model.model_json_schema(),
            side_effect=side_effect,
        )
        self._tools[name] = RegisteredHarnessTool(
            spec=spec,
            argument_model=argument_model,
            handler=handler,
        )
        return spec

    def get(self, name: str) -> RegisteredHarnessTool | None:
        return self._tools.get(name)

    def specs(self) -> Sequence[HarnessToolSpec]:
        return tuple(tool.spec for tool in self._tools.values())

    def model_tools(self) -> list[dict[str, Any]]:
        return [spec.as_model_tool() for spec in self.specs()]

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)


__all__ = [
    "HarnessRegistry",
    "HarnessToolHandler",
    "RegisteredHarnessTool",
]
