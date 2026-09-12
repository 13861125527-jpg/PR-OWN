"""领域核心模型（domain/models.py）。

纯 Pydantic 模型，无 IO / SDK 依赖。契约来源：05-domain-data-design.md。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from .enums import (
    ChangedFileStatus,
    ChangeRequestSource,
    ContextLayer,
    ContextSourceKind,
    CoverageReason,
    DiffLineType,
    EvidenceKind,
    FindingSourceKind,
    L3HitReason,
    ModelUsageOutcome,
    StageName,
    SymbolKind,
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
    "AgentToolRequest",
    "AgentBudget",
    "AgentSessionSnapshot",
    "ToolDefinition",
    "ToolCall",
    "ToolResult",
    "ModelUsage",
    "GlobalBudget",
    "GateFeatures",
    "SymbolDef",
    "L3Hit",
    "utcnow",
    "is_safe_repo_path",
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


def is_safe_repo_path(path: str) -> bool:
    """仓库相对路径：拒绝空、绝对、盘符、UNC、目录穿越。"""
    if not path:
        return False
    if path.startswith(("\\", "/")):
        return False
    if path.startswith("\\\\") or path.startswith("//"):
        return False
    if len(path) >= 2 and path[1] == ":":
        return False
    normalized = path.replace("\\", "/")
    return ".." not in normalized.split("/")


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
    hunks: list[DiffHunk] = Field(default_factory=list)

    @field_validator("path", "old_path")
    @classmethod
    def _path_safe(cls, v: str | None) -> str | None:
        """P3-3/P1（复验）：path 与 old_path 共用同一路径安全校验。

        规范化分隔符后拒绝空路径、绝对路径、盘符、UNC、目录穿越；None 放行。
        """
        if v is None:
            return v
        if not v:
            raise ValueError("path 不能为空")
        if v.startswith(("\\", "/")):
            raise ValueError(f"path 不能是绝对路径: {v}")
        if v.startswith("\\\\") or v.startswith("//"):
            raise ValueError(f"path 不能是 UNC/网络路径: {v}")
        if len(v) >= 2 and v[1] == ":":
            raise ValueError(f"path 不能包含盘符: {v}")
        normalized = v.replace("\\", "/")
        if ".." in normalized.split("/"):
            raise ValueError(f"path 不能包含目录穿越: {v}")
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
    start_line: int | None = None
    end_line: int | None = None


class ReviewContext(BaseModel):
    """一次审查的上下文（L0–L4 装配结果）。"""

    run_id: str
    chunks: list[ContextChunk] = Field(default_factory=list)
    total_tokens: int = 0
    budget_tokens: int = 0
    truncated: bool = False

    def assert_within_budget(self) -> None:
        """硬断言：装配结果不得超预算（唯一例外是 L0 治理文本本身超预算，见装配器）。"""
        if self.total_tokens > self.budget_tokens:
            raise ValueError(
                f"ReviewContext 超预算：{self.total_tokens} > {self.budget_tokens} tokens"
            )


class CoverageItem(BaseModel):
    """覆盖条目（统一结构，P1-9；自 models 供 run/context 共用，避免分层环依赖）。"""

    target: str = Field(description="文件路径 / 角色 id / agent task id")
    reason: CoverageReason = CoverageReason.COVERED
    stage: StageName = StageName.REVIEW
    detail: str | None = None


class CoverageManifest(BaseModel):
    """覆盖清单（强制输出项，07 §8）。"""

    items: list[CoverageItem] = Field(default_factory=list)
    truncated: bool = False


class GateFeatures(BaseModel):
    """文件级门控事实（V2-A；CONTEXT 从 ChangedFile 抽取，禁止解析 prompt）。"""

    path: str
    language: str | None = None
    status: ChangedFileStatus = ChangedFileStatus.MODIFIED
    path_parts: list[str] = Field(default_factory=list)
    added_imports: list[str] = Field(default_factory=list)
    keyword_hits: list[str] = Field(default_factory=list)
    combo_hits: list[str] = Field(default_factory=list)
    has_added_lines: bool = False


class SymbolDef(BaseModel):
    """仓库内一个符号定义（V2-B；结构元数据，不是模型推理）。"""

    name: str
    qualname: str
    kind: SymbolKind
    path: str
    start_line: int
    end_line: int
    signature: str = ""


class L3Hit(BaseModel):
    """一条最小 L3 检索命中。"""

    symbol: SymbolDef
    reason: L3HitReason
    snippet: str = ""
    notes: str | None = None


class ReviewUnit(BaseModel):
    """单次模型调用的上下文单位（04 §1 per-file map-reduce 的原子任务粒度）。

    一个 changed file 可产出多个 unit（大文件超单次预算时分块），每个 unit 自包含
    L0/L1/L2/L4，且满足：context.total_tokens <= input_limit（输入预算硬约束）且
    output_reserve_tokens >= 配置的输出预留。
    """

    unit_id: str
    file_path: str
    context: ReviewContext
    truncated: bool = Field(description="本 unit 内是否真实丢失内容（L1/L4 裁剪）")
    coverage: CoverageManifest
    input_limit: int = Field(description="本 unit 输入预算上限（总窗口 - 输出预留）")
    output_reserve_tokens: int = Field(description="本 unit 保留的模型输出空间")
    total_window_tokens: int = Field(description="模型总窗口")
    gate_features: GateFeatures | None = None


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


class AgentToolRequest(BaseModel):
    """模型本轮请求的一件工具（含 provider call id）。"""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentMessage(BaseModel):
    """Agent 消息（V3）。"""

    role: str = Field(description="system | user | assistant | tool")
    content: str = ""
    reasoning_content: str | None = None
    is_compressed: bool = False
    summary_ref: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[AgentToolRequest] = Field(default_factory=list)


class AgentSessionSnapshot(BaseModel):
    """只读会话视图（Provider 不得修改）。"""

    model_config = ConfigDict(frozen=True)

    session_id: str
    mode: str
    messages: tuple[AgentMessage, ...] = ()
    remaining_rounds_value: int = 0
    budget_summary_value: dict[str, Any] = Field(default_factory=dict)

    def remaining_rounds(self) -> int:
        return self.remaining_rounds_value

    def budget_summary(self) -> dict[str, Any]:
        return dict(self.budget_summary_value)


class AgentBudget(BaseModel):
    """Agent 预算（V3，08 §5：reserved finalize budget）。"""

    max_rounds: int = 8
    max_tool_calls: int = 12
    max_tool_attempts: int = 36
    max_tokens: int = 0
    max_cost_usd: float = 0.0
    reserved_finalize_tokens: int = 0
    reserved_finalize_usd: float = 0.0
    max_wallclock_s: int = 300
    grace_rounds: int = 2
    repeat_threshold: int = 3
    compact_threshold_ratio: float = 0.60
    compact_keep_rounds: int = 2
    max_session_chars: int = 200_000


class ToolDefinition(BaseModel):
    """工具定义（V3，08 §2）。"""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict, description="JSON Schema")
    permissions: ToolPermission = ToolPermission.READ_ONLY
    result_limit: int = 200
    timeout_s: int = 10
    max_result_chars: int = Field(default=16000, ge=512)


class ToolCall(BaseModel):
    """工具调用（状态与结果分离，05 §2）。"""

    tool_call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: ToolCallStatus = ToolCallStatus.OK
    task_id: str | None = None
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


class BudgetReservation:
    """一次发送前最坏预留（P0-1：并发安全原子预留，10 §7 硬预算）。"""

    def __init__(
        self,
        *,
        input_tokens: int,
        max_output_tokens: int,
        est_cost_usd: float | None,
    ) -> None:
        self.input_tokens = input_tokens
        self.max_output_tokens = max_output_tokens
        self.est_cost_usd = est_cost_usd  # None = 未定价（费用门控标记 unknown）
        self.settled = False
        self.settled_input = 0
        self.settled_output = 0
        self.settled_cost = 0.0


class GlobalBudget(BaseModel):
    """全局硬预算（P2-2：admission control，见 10 §7 / 08 §5）。

    Token 为请求前可控硬上限；费用为保守估算（最坏预留）。
    P0-1（V1-d 返工）：并发安全的"检查并预留"——reserve() 原子预扣
    input+max_output token 与最坏费用，请求完成/失败后 settle() 按实际
    结算并释放未用预留；墙钟到期拒绝新请求。
    """

    model_config = ConfigDict(extra="forbid")

    max_total_tokens: int = Field(default=80000, gt=0)
    max_cost_usd: float = Field(default=2.0, gt=0.0)
    max_runtime_seconds: int = Field(default=600, gt=0)
    reserved_finalize_ratio: float = Field(default=0.10, ge=0.0, le=1.0)
    tokens_used: int = 0
    cost_used: float = 0.0
    started_at: datetime = Field(default_factory=utcnow)
    # 定价状态（V1-d P0-1 / 三轮/四轮）：known=按实际 token 结算；estimated=无 token 数据按预留入账；
    # unknown=未配置价格（费用门控无法验证）；overrun=实际费用超预留（熔断，停止后续调用）
    pricing_status: str = Field(default="known", description="known | estimated | unknown | overrun")
    # overrun 熔断（V1-d 四轮）：真实费用超过预留估算时置 True，reserve() 拒绝所有新请求。
    # 请求一旦发出费用无法撤销，硬预算只能做"请求前保守预留 + 意外超估立即熔断"。
    overrun: bool = False

    _lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    _reserved_tokens: int = PrivateAttr(default=0)
    _reserved_cost: float = PrivateAttr(default=0.0)

    @property
    def exploration_tokens(self) -> int:
        """探索额度 = 总预算 × (1 - 预留比例)。"""
        return int(self.max_total_tokens * (1 - self.reserved_finalize_ratio))

    @property
    def remaining_runtime_seconds(self) -> float:
        """剩余墙钟（用于 in-flight 调用超时）。"""
        return max(0.0, self.max_runtime_seconds - (utcnow() - self.started_at).total_seconds())

    async def reserve(
        self,
        *,
        input_tokens: int,
        max_output_tokens: int,
        est_cost_usd: float | None,
        protect_tokens: int = 0,
        protect_cost: float = 0.0,
    ) -> BudgetReservation | None:
        """发送前原子预留（admission control，P0-1）。

        - Token：input + max_output 不超（已用 + 已预留 + 保护额度）；
        - 费用：est_cost_usd 提供时校验 cost_used + 预留 + est + 保护额度 ≤ max_cost_usd；
          未提供（未定价）→ 跳过费用门控并标记 pricing_status=unknown（不伪造零成本）；
        - 墙钟：到期后拒绝新请求（10 §7）。
        任一超限返回 None（调用方不得发请求）。
        ``protect_*`` 用于为共享 finalize 池留出门槛（V3-B）；默认 0 保持 V1 行为。
        """
        async with self._lock:
            if self.overrun:
                return None  # V1-d 四轮：费用超预留后熔断，停止所有新请求
            if self._wall_clock_expired():
                return None
            if (
                self.tokens_used
                + self._reserved_tokens
                + input_tokens
                + max_output_tokens
                + max(0, protect_tokens)
                > self.max_total_tokens
            ):
                return None
            if est_cost_usd is not None:
                if (
                    self.cost_used
                    + self._reserved_cost
                    + est_cost_usd
                    + max(0.0, protect_cost)
                    > self.max_cost_usd
                ):
                    return None
            else:
                self.pricing_status = "unknown"  # 未定价：费用门控无法验证
            reservation = BudgetReservation(
                input_tokens=input_tokens,
                max_output_tokens=max_output_tokens,
                est_cost_usd=est_cost_usd,
            )
            if est_cost_usd is not None:
                self._reserved_cost += est_cost_usd
            self._reserved_tokens += input_tokens + max_output_tokens
            return reservation

    async def settle(
        self,
        reservation: BudgetReservation,
        *,
        actual_input: int,
        actual_output: int,
        actual_cost: float,
    ) -> None:
        """结算：释放预留，按实际 usage 入账（失败/取消同样必须调用，避免泄漏）。"""
        async with self._lock:
            if reservation.settled:
                return
            reservation.settled = True
            reservation.settled_input = actual_input
            reservation.settled_output = actual_output
            reservation.settled_cost = actual_cost
            self._reserved_tokens = max(
                0, self._reserved_tokens - reservation.input_tokens - reservation.max_output_tokens
            )
            if reservation.est_cost_usd is not None:
                self._reserved_cost = max(0.0, self._reserved_cost - reservation.est_cost_usd)
            self.tokens_used += actual_input + actual_output
            self.cost_used += actual_cost

    def _wall_clock_expired(self) -> bool:
        return (utcnow() - self.started_at).total_seconds() >= self.max_runtime_seconds

    def can_admit(
        self,
        input_tokens: int,
        max_output_tokens: int,
        est_cost_usd: float | None = None,
    ) -> bool:
        """同步检查（非并发场景/测试用；并发路径请用 reserve）。"""
        if self.tokens_used + input_tokens + max_output_tokens > self.max_total_tokens:
            return False
        return not (est_cost_usd is not None and self.cost_used + est_cost_usd > self.max_cost_usd)

    def consume(self, usage: ModelUsage) -> None:
        """调用后入账（含 late_cancelled 的迟到响应；并发路径用 settle）。"""
        self.tokens_used += usage.input_tokens + usage.output_tokens
        self.cost_used += usage.cost_usd

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.max_total_tokens - self.tokens_used - self._reserved_tokens)

    @property
    def remaining_cost(self) -> float:
        return max(0.0, self.max_cost_usd - self.cost_used - self._reserved_cost)


# ChangedFile.hunks 前向引用 DiffHunk（定义于文件后部），类全部定义后重建
ChangedFile.model_rebuild()
