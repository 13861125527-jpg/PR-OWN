"""Provider 抽象协议（domain/protocols.py）。

方向：domain 定义抽象，providers/ 实现并依赖 domain；domain 不得依赖具体 Provider 实现。
"""

from __future__ import annotations

from typing import Any, Protocol

from .models import ChangeRequest, ModelUsage
from .run import PublishPlan


class GitProvider(Protocol):
    """Git 提供者（github_api / local_git / fake）。"""

    async def get_changes(self, ref: str) -> ChangeRequest:
        """返回变更元数据 + 锁定 head SHA（github_pr / local_range）。"""
        ...

    async def get_diff(self, base_sha: str, head_sha: str, paths: list[str] | None = None) -> str:
        """返回 unified diff 原始文本。"""
        ...

    async def publish_comments(self, plan: PublishPlan) -> dict[str, Any]:
        """按 Saga 发布评论；返回逐条 remote_comment_id 与状态。"""
        ...


class ModelResponse:
    """LLM 响应载体（结构化输出或工具动作）。"""

    def __init__(
        self,
        text: str = "",
        data: dict[str, Any] | None = None,
        usage: ModelUsage | None = None,
        action: str | None = None,
    ) -> None:
        self.text = text
        self.data = data
        self.usage = usage
        self.action = action  # None | tool_call | submit_finding | finish_review


class LLMProvider(Protocol):
    """LLM 提供者（openai_compat / fake）。能力需实测（14 OQ-1）。"""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.1,
    ) -> ModelResponse:
        """通用补全；schema 支持时返回结构化 data。"""
        ...

    async def structured(
        self,
        messages: list[dict[str, Any]],
        *,
        prompt_hash: str | None = None,
        schema_hash: str | None = None,
    ) -> tuple[list[Any], ModelUsage]:
        """结构化输出（V1 reviewer 用）；返回 (候选列表, usage)。"""
        ...

    async def tool_loop(
        self,
        session: object,
        tools: list[dict[str, Any]],
        *,
        budget: dict[str, Any],
    ) -> ModelResponse:
        """V3 工具循环（原生 tool calling 或受限 Action JSON 协议）。"""
        ...


class Storage(Protocol):
    """存储抽象（SQLite 实现）。"""

    async def record_run(self, run: object) -> None: ...

    async def record_findings(self, findings: list[Any]) -> None: ...

    async def record_usage(self, usage: ModelUsage) -> None: ...
