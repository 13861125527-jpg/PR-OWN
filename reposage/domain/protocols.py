"""Provider 抽象协议（domain/protocols.py）。

方向：domain 定义抽象，providers/ 实现并依赖 domain；domain 不得依赖具体 Provider 实现。
V1 使用的边界一律使用具体类型；V3 专属（tool_loop 的 session/tools）暂以 object/dict 前向声明。
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from .finding import Finding, FindingCandidate
from .models import ChangeRequest, GlobalBudget, ModelUsage, ReviewContext
from .run import PublishCommentResult, PublishPlan, ReviewRun


class GitProvider(Protocol):
    """Git 提供者（github_api / local_git / fake）。"""

    async def get_changes(self, ref: str) -> ChangeRequest:
        """返回变更元数据 + 锁定 head SHA（github_pr / local_range）。"""
        ...

    async def get_diff(self, base_sha: str, head_sha: str, paths: list[str] | None = None) -> str:
        """返回 unified diff 原始文本。"""
        ...

    async def publish_comments(self, plan: PublishPlan) -> dict[str, PublishCommentResult]:
        """按 Saga 发布评论；返回逐条状态（含 remote_comment_id / 失败）。"""
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
        schema: type[BaseModel] | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        """通用补全；schema 为 Pydantic 模型类时本地校验并把 model_dump 写入 data。"""
        ...

    async def structured(
        self,
        messages: list[dict[str, Any]],
        *,
        prompt_hash: str | None = None,
        schema_hash: str | None = None,
    ) -> tuple[list[FindingCandidate], ModelUsage]:
        """结构化输出（V1 reviewer 用）；返回 (候选列表, usage)。"""
        ...

    async def tool_loop(
        self,
        session: object,
        tools: list[dict[str, Any]],
        *,
        budget: GlobalBudget,
    ) -> ModelResponse:
        """V3 工具循环（原生 tool calling 或受限 Action JSON 协议）。

        session/tools 为 V3 前向声明：实现时替换为 AgentSession / ToolDefinition。
        """
        ...


class Storage(Protocol):
    """存储抽象（async；SQLite 实现内部用 to_thread 避免阻塞事件循环）。"""

    async def record_run(self, run: ReviewRun) -> None: ...

    async def record_findings(self, findings: list[Finding]) -> None: ...

    async def record_usage(self, run_id: str, usage: ModelUsage) -> None: ...


__all__ = [
    "GitProvider",
    "LLMProvider",
    "ModelResponse",
    "Storage",
    "GlobalBudget",
    "ReviewContext",
]
