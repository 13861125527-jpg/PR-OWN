"""工具 Registry（V3-A）。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from reposage.domain.enums import ToolCallStatus
from reposage.domain.models import ToolDefinition
from reposage.tools.snapshot import ToolWorkspace

Handler = Callable[[ToolWorkspace, BaseModel], Awaitable["HandlerOutput"]]


@dataclass
class HandlerOutput:
    """handler 语义结果；信封再做最终字符硬上限。"""

    payload: Any = None
    truncated: bool = False
    error: str | None = None
    status: ToolCallStatus = ToolCallStatus.OK


@dataclass
class ToolEntry:
    definition: ToolDefinition
    args_model: type[BaseModel]
    handler: Handler


class ToolRegistry:
    """name 唯一；重复注册启动期失败。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}

    def register(
        self,
        definition: ToolDefinition,
        args_model: type[BaseModel],
        handler: Handler,
    ) -> None:
        if definition.name in self._tools:
            raise ValueError(f"duplicate tool: {definition.name}")
        self._tools[definition.name] = ToolEntry(definition, args_model, handler)

    def get(self, name: str) -> ToolEntry | None:
        return self._tools.get(name)

    def enabled(self) -> list[ToolDefinition]:
        return [e.definition for e in self._tools.values()]

    def schemas(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for entry in self._tools.values():
            out.append(
                {
                    "name": entry.definition.name,
                    "description": entry.definition.description,
                    "parameters": entry.definition.parameters,
                }
            )
        return out

    def parse_args(self, name: str, args: dict[str, Any]) -> BaseModel:
        entry = self._tools[name]
        return entry.args_model.model_validate(args)


def validation_error_code(exc: ValidationError) -> str:
    return "invalid_args"
