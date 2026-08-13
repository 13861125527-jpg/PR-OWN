"""领域核心模型（domain/models.py）。

纯 Pydantic 模型，无 IO / SDK 依赖。契约来源：05-domain-data-design.md。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .enums import (
    ChangedFileStatus,
    ChangeRequestSource,
    ContextLayer,
    ContextSourceKind,
    DiffLineType,
    EvidenceKind,
    FindingSourceKind,
    ModelUsageOutcome,
    ToolCallStatus,
    ToolPermission,
)

__all__ = [
    "RepositoryRef",
    "CommitRef",
    "ChangeRequest",
    "ChangedFile",
    "DiffHunk",
    "DiffLine",
    "ContextSource",
    "ContextChunk",
    "ReviewContext",
    "Evidence",
    "FindingSource",
    "AgentMessage",
    "AgentBudget",
    "ToolDefinition",
    "ToolCall",
    "ToolResult",
    "ModelUsage",
    "GlobalBudget",
    "utcnow",
    "ChangedFileStatus",
    "ChangeRequestSource",
    "ContextLayer",
    "ContextSourceKind",
    "DiffLineType",
    "EvidenceKind",
    "FindingSourceKind",
    "ModelUsageOutcome",
    "ToolCallStatus",
    "ToolPermission",
]


def utcnow() -> datetime:
    return datetime.now(UTC)


class RepositoryRef(BaseModel):
    """仓库引用。"""

    provider: str = Field(description="github | local")
    owner: str | None = Field(default=None, description="GitHub 时必填")
    name: str | None = Field(default=None, description="GitHub 时必填")
    local_path: Path | None = Field(default=None, description="local 时必填，绝对路径")
    default_branch: str = Field(default="main")

    @field_validator("local_path")
    @classmethod
    def _local_path_absolute(cls, v: Path | None) -> Path | None:
        if v is not None and not v.is_absolute():
            raise ValueError("local_path 必须是绝对路径")
        return v

    @model_validator(mode="after")
    def _check_source_fields(self) -> RepositoryRef:
        """P2-1：github 必须 owner/name；local 必须绝对 local_path。"""
        if self.provider == "github" and (not self.owner or not self.name):
            raise ValueError("github 模式必须提供 owner 与 name")
        if self.provider == "local" and self.local_path is None:
            raise ValueError("local 模式必须提供绝对 local_path")
        return self


class CommitRef(BaseModel):
    """提交引用。"""

    sha: str = Field(min_length=1, max_length=64, description="GitHub 模式为 SHA（≥7 或完整）；本地模式可为分支/ref 名")
    branch: str | None = None
    label: str | None = Field(default=None, description="base | head")
    locked: bool = Field(default=False, description="head 必须 locked=true 才能审查")


class ChangeRequest(BaseModel):
    """统一本地/远程输入来源（P1-R2-3）。"""

    source: ChangeRequestSource
    external_id: str | None = Field(default=None, description="GitHub PR number；local_range 为 None")
    base: CommitRef
    head: CommitRef = Field(description="head SHA 全程锁定；审查前置 locked=true")
    title: str | None = None
    description: str | None = None
    author: str | None = None
    is_draft: bool | None = None
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)

    def lock_head(self) -> ChangeRequest:
        return self.model_copy(update={"head": self.head.model_copy(update={"locked": True})})

    @model_validator(mode="after")
    def _source_consistency(self) -> ChangeRequest:
        """P2-1：github_pr 必须 external_id；local_range 必须 external_id=None；label 一致。"""
        if self.source is ChangeRequestSource.GITHUB_PR and not self.external_id:
            raise ValueError("github_pr 模式必须提供 external_id")
        if self.source is ChangeRequestSource.LOCAL_RANGE and self.external_id is not None:
            raise ValueError("local_range 模式 external_id 必须为 None")
        if self.head.label not in (None, "head"):
            raise ValueError("head.label 必须为 'head'")
        if self.base.label not in (None, "base"):
            raise ValueError("base.label 必须为 'base'")
        return self

    def require_head_locked(self) -> ChangeRequest:
        """审查前置检查：head 未锁定则拒绝（目标 SHA 锁定铁律）。"""
        if not self.head.locked:
            raise ValueError("head 未锁定：审查前必须 lock_head()（目标 SHA 锁定铁律）")
        return self


class ChangedFile(BaseModel):
    """变更文件（diff parser 产出）。"""

    path: str = Field(description="仓库相对路径，禁止 ../ 或绝对路径")
    status: ChangedFileStatus
    old_path: str | None = None
    language: str | None = None
    is_binary: bool = False
    is_generated: bool = False
    is_locked: bool = False
    additions: int = 0
    deletions: int = 0
    size_bytes: int = 0

    @field_validator("path")
    @classmethod
    def _path_safe(cls, v: str) -> str:
        if v.startswith("/") or ".." in v.split("/"):
            raise ValueError(f"path 必须是仓库相对路径: {v}")
        return v


class DiffLine(BaseModel):
    """diff 行（P2-1：added 必须有 new_ln，removed 必须有 old_ln）。"""

    type: DiffLineType
    old_ln: int | None = None
    new_ln: int | None = None
    content: str = ""
    is_blank: bool = False

    @model_validator(mode="after")
    def _line_number_consistency(self) -> DiffLine:
        if self.type is DiffLineType.ADDED and self.new_ln is None:
            raise ValueError("added 行必须提供 new_ln")
        if self.type is DiffLineType.REMOVED and self.old_ln is None:
            raise ValueError("removed 行必须提供 old_ln")
        return self


class DiffHunk(BaseModel):
    """diff hunk。"""

    hunk_id: str
    header: str = Field(description="@@ -a,b +c,d @@")
    new_start: int
    new_count: int
    old_start: int
    old_count: int
    lines: list[DiffLine] = Field(default_factory=list)

    @property
    def added_lines(self) -> list[int]:
        """全部新增行的新行号（评论候选锚点）。"""
        return [ln.new_ln for ln in self.lines if ln.type is DiffLineType.ADDED and ln.new_ln is not None]


class ContextSource(BaseModel):
    """上下文来源。"""

    kind: ContextSourceKind
    ref: str = Field(description="如 file:src/a.py / symbol:foo / rule:python.01")
    sha: str | None = None


class ContextChunk(BaseModel):
    """上下文块（token-aware 分块，见 06 §2）。"""

    layer: ContextLayer
    source: ContextSource
    content: str
    tokens: int = 0
    truncated: bool = False


class ReviewContext(BaseModel):
    """一次审查的上下文（L0–L4 装配结果）。"""

    run_id: str
    chunks: list[ContextChunk] = Field(default_factory=list)
    total_tokens: int = 0
    budget_tokens: int = 0
    truncated: bool = False


class Evidence(BaseModel):
    """证据（程序标记 verified；模型不能自证）。"""

    kind: EvidenceKind
    location: str = Field(description="文件+行 或 符号")
    content: str = ""
    tool_call_id: str | None = None
    verified: bool = False


class FindingSource(BaseModel):
    """Finding 来源（可多来源合并，05 §3 sources: list）。"""

    kind: FindingSourceKind
    role_id: str | None = None
    analyzer_id: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    verified_by: str = Field(default="program", description="program | model（事实字段只能 program）")


class AgentMessage(BaseModel):
    """Agent 消息（V3）。"""

    role: str = Field(description="system | user | assistant | tool")
    content: str
    is_compressed: bool = False
    summary_ref: str | None = None


class AgentBudget(BaseModel):
    """Agent 预算（V3，08 §5：reserved finalize budget）。"""

    max_rounds: int = 8
    max_tool_calls: int = 12
    max_tokens: int = 0
    max_cost_usd: float = 0.0
    reserved_finalize_tokens: int = 0
    reserved_finalize_usd: float = 0.0
    max_wallclock_s: int = 300
    grace_rounds: int = 2
    repeat_threshold: int = 3


class ToolDefinition(BaseModel):
    """工具定义（V3，08 §2）。"""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict, description="JSON Schema")
    permissions: ToolPermission = ToolPermission.READ_ONLY
    result_limit: int = 200
    timeout_s: int = 10


class ToolCall(BaseModel):
    """工具调用（状态与结果分离，05 §2）。"""

    tool_call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: ToolCallStatus = ToolCallStatus.OK
    repeat_of: str | None = None
    tokens_used: int = 0
    attempt_count: int = 1


class ToolResult(BaseModel):
    """工具返回（与 ToolCall 分离建模）。"""

    tool_call_id: str
    data: str | None = None
    error: str | None = None
    truncated: bool = False
    source: ContextSource | None = None
    tokens: int = 0


class ModelUsage(BaseModel):
    """模型调用记账（P0-R2-3：迟到响应也记账）。"""

    model: str
    role: str = Field(description="general/security/judge/agent…")
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    outcome: ModelUsageOutcome = ModelUsageOutcome.COMPLETED
    latency_ms: int = 0
    retries: int = 0
    schema_repairs: int = 0
    prompt_hash: str | None = None
    schema_hash: str | None = None


class GlobalBudget(BaseModel):
    """全局硬预算（P2-2：admission control，见 10 §7 / 08 §5）。

    Token 为请求前可控硬上限；费用为保守估算（最坏预留）。
    """

    max_total_tokens: int = Field(default=80000, gt=0)
    max_cost_usd: float = Field(default=2.0, gt=0.0)
    max_runtime_seconds: int = Field(default=600, gt=0)
    reserved_finalize_ratio: float = Field(default=0.10, ge=0.0, le=1.0)
    tokens_used: int = 0
    cost_used: float = 0.0
    started_at: datetime = Field(default_factory=utcnow)

    @property
    def exploration_tokens(self) -> int:
        """探索额度 = 总预算 × (1 - 预留比例)。"""
        return int(self.max_total_tokens * (1 - self.reserved_finalize_ratio))

    def can_admit(
        self,
        input_tokens: int,
        max_output_tokens: int,
        est_cost_usd: float | None = None,
    ) -> bool:
        """发送前最坏预留（admission control，10 §7 / P0-R2-3）。

        - Token 维度：input + max_output 不超剩余额度；
        - 费用维度（P2 复验补充）：提供 est_cost_usd 时同时校验 cost_used + est ≤ max_cost_usd。
        retry 与 finalize reserve 的费用预留为 V1 调用真实模型前的前置任务（见 14 OQ-1）。
        """
        if self.tokens_used + input_tokens + max_output_tokens > self.max_total_tokens:
            return False
        return not (est_cost_usd is not None and self.cost_used + est_cost_usd > self.max_cost_usd)

    def consume(self, usage: ModelUsage) -> None:
        """调用后入账（含 late_cancelled 的迟到响应）。"""
        self.tokens_used += usage.input_tokens + usage.output_tokens
        self.cost_used += usage.cost_usd

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.max_total_tokens - self.tokens_used)

    @property
    def remaining_cost(self) -> float:
        return max(0.0, self.max_cost_usd - self.cost_used)
