"""FakeLLMProvider — 录制回放/脚本化响应，供测试与评测（11 §6）。

不访问网络；响应由脚本或录制数据驱动，使测试确定、快速。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from reposage.domain.models import ModelUsage
from reposage.domain.protocols import AgentSessionView, ModelResponse


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
        role_findings: dict[str, list[Any]] | None = None,
        input_tokens: int = 100,
        output_tokens: int = 50,
        cost_usd: float = 0.001,
        handler: Callable[[list[dict[str, Any]]], Any] | None = None,
        require_content: str | None = None,
        tool_script: Sequence[ModelResponse] | None = None,
    ) -> None:
        self.default_findings = default_findings or []
        self.role_findings = role_findings or {}
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.handler = handler  # 自定义回调：messages -> data
        self.require_content = require_content
        self.calls: list[dict[str, Any]] = []  # 调用记录（断言用）
        self.in_flight = 0
        self.max_in_flight = 0
        self._tool_script = list(tool_script or [])
        self._tool_i = 0

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
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.calls.append(
            {"kind": "structured", "messages": messages, "prompt_hash": prompt_hash, "schema_hash": schema_hash}
        )
        try:
            role_id = _role_id_from_messages(messages)
            if self.require_content is not None:
                blob = "\n".join(str(m.get("content", "")) for m in messages)
                if self.require_content not in blob:
                    return [], self._usage()
            findings = self.role_findings.get(role_id, self.default_findings)
            return list(findings), self._usage()
        finally:
            self.in_flight -= 1

    async def tool_loop(
        self,
        session: AgentSessionView,
        tools: list[dict[str, Any]],
        *,
        budget: Any,
        protocol: str = "native",
    ) -> ModelResponse:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.calls.append(
            {
                "kind": "tool_loop",
                "tools": tools,
                "budget": budget,
                "protocol": protocol,
                "session_id": getattr(session, "session_id", ""),
            }
        )
        try:
            if self._tool_i < len(self._tool_script):
                resp = self._tool_script[self._tool_i]
                self._tool_i += 1
                if resp.usage is None:
                    resp.usage = self._usage()
                return resp
            return ModelResponse(
                text="",
                action="finish_review",
                data={"name": "finish_review", "arguments": {"reason": "script_exhausted"}},
                usage=self._usage(),
            )
        finally:
            self.in_flight -= 1


def _role_id_from_messages(messages: list[dict[str, Any]]) -> str:
    """从可信 system 角色块读取 `# role: <id>`；没有则 general（V1 SinglePass）。"""
    for message in messages:
        if message.get("role") != "system":
            continue
        for line in str(message.get("content", "")).splitlines():
            stripped = line.strip()
            if stripped.startswith("# role:"):
                return stripped.split(":", 1)[1].strip()
    return "general"
