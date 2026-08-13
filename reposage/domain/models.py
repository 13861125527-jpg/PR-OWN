"""领域核心模型（domain/models.py）。

纯 Pydantic 模型，无 IO / SDK 依赖。契约来源：05-domain-data-design.md。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

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
    """diff 行。"""

    type: DiffLineType
    old_ln: int | None = None
    new_ln: int | None = None
    content: str = ""
    is_blank: bool = False


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
