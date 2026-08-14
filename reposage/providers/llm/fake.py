"""FakeLLMProvider — 录制回放/脚本化响应，供测试与评测（11 §6）。

不访问网络；响应由脚本或录制数据驱动，使测试确定、快速。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from reposage.domain.models import ModelUsage
from reposage.domain.protocols import ModelResponse


class FakeLLMProvider:
    """脚本化 LLM 提供者。

    用法::

        fake = FakeLLMProvider(default_findings=[FindingCandidate(...)])
        resp = await fake.structured([...])
    """

    def __init__(
        self,
        *,
        default_findings: list[Any] | None = None,
        input_tokens: int = 100,
        output_tokens: int = 50,
        cost_usd: float = 0.001,
        handler: Callable[[list[dict[str, Any]]], Any] | None = None,
    ) -> None:
        self.default_findings = default_findings or []
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.handler = handler  # 自定义回调：messages -> data
        self.calls: list[dict[str, Any]] = []  # 调用记录（断言用）

    def _usage(self, outcome: str = "completed") -> ModelUsage:
        return ModelUsage(
            model="fake-model",
            role="test",
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd,
            outcome=outcome,  # type: ignore[arg-type]
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: Any | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        self.calls.append({"kind": "complete", "messages": messages, "schema": schema})
        if self.handler is not None:
            return ModelResponse(data=self.handler(messages), usage=self._usage())
        return ModelResponse(text="", data={}, usage=self._usage())

    async def structured(
        self,
        messages: list[dict[str, Any]],
        *,
        prompt_hash: str | None = None,
        schema_hash: str | None = None,
    ) -> tuple[list[Any], ModelUsage]:
        self.calls.append(
            {"kind": "structured", "messages": messages, "prompt_hash": prompt_hash, "schema_hash": schema_hash}
        )
        return list(self.default_findings), self._usage()

    async def tool_loop(
        self,
        session: object,
        tools: list[dict[str, Any]],
        *,
        budget: Any,
    ) -> ModelResponse:
        self.calls.append({"kind": "tool_loop", "tools": tools, "budget": budget})
        return ModelResponse(text="", action="finish_review", usage=self._usage())
