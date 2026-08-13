"""运行、覆盖与发布计划模型（domain/run.py）。

契约来源：05 §2（ReviewRun/StageResult/CoverageManifest/PublishPlan/CommentPlan/PublishOperation）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .enums import (
    CommentKind,
    CommentStatus,
    CoverageReason,
    FeedbackKind,
    PublishOperationKind,
    PublishOperationStatus,
    PublishPlanStatus,
    ReviewRunStatus,
    ReviewStrategyName,
    ReviewTaskKind,
    ReviewTaskStatus,
    StageName,
    StageStatus,
)
from .models import utcnow


class StageResult(BaseModel):
    """阶段 trace 结果（P1-R2-4：required 归并标记）。"""

    stage: StageName
    status: StageStatus = StageStatus.OK
    required: bool = True
    duration_ms: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    detail: str | None = None


class CoverageItem(BaseModel):
    """覆盖条目（统一结构，P1-9）。"""

    target: str = Field(description="文件路径 / 角色 id / agent task id")
    reason: CoverageReason = CoverageReason.COVERED
    stage: StageName = StageName.REVIEW
    detail: str | None = None


class CoverageManifest(BaseModel):
    """覆盖清单（强制输出项，07 §8）。"""

    items: list[CoverageItem] = Field(default_factory=list)
    truncated: bool = False


class ReviewTask(BaseModel):
    """审查任务（V1 file_review / V2 role_review / V3 agent_task）。"""

    task_id: str
    run_id: str
    kind: ReviewTaskKind
    target: str = Field(description="文件路径或角色 id")
    status: ReviewTaskStatus = ReviewTaskStatus.PENDING
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None


class RoleSpec(BaseModel):
    """角色注册表条目（V2，07 §2）。"""

    id: str = Field(description="如 security")
    gate: str = Field(description="确定性门控规则描述（实现为纯函数）")
    model_requirement: str = Field(default="general", description="路由能力档")
    enabled: bool = True
    required: bool = False  # optional 角色失败不放大为 PARTIAL（P1-R2-4）


class SourceRunResult(BaseModel):
    """Strategy 产出（与 CandidateFinding 一起返回，03 §5）。"""

    strategy: ReviewStrategyName
    tasks: list[ReviewTask] = Field(default_factory=list)
    usages: list[dict[str, Any]] = Field(default_factory=list, description="ModelUsage 序列化列表")
    warnings: list[str] = Field(default_factory=list)


class CommentPlan(BaseModel):
    """单条评论计划（P0-R2-2：required 标注）。"""

    comment_id: str
    required: bool = True
    finding_occurrence_id: str | None = None
    fingerprint: str | None = None
    kind: CommentKind = CommentKind.INLINE
    path: str | None = None
    line: int | None = None
    body: str = ""
    marker: str = ""
    remote_comment_id: int | None = None
    status: CommentStatus = CommentStatus.PREPARED


class PublishOperation(BaseModel):
    """发布独立任务（supersede 清理等，P0-R2-2）。"""

    op_id: str
    kind: PublishOperationKind = PublishOperationKind.SUPERSEDE_CLEANUP
    status: PublishOperationStatus = PublishOperationStatus.PENDING
    detail: str | None = None


class PublishPlan(BaseModel):
    """发布计划（Saga/Outbox 状态机，07 §7）。"""

    plan_id: str
    run_id: str
    mode: str = Field(default="dry_run", description="dry_run | publish")
    status: PublishPlanStatus = PublishPlanStatus.PREPARED
    summary_comment: str | None = None
    comments: list[CommentPlan] = Field(default_factory=list)
    operations: list[PublishOperation] = Field(default_factory=list)
    watermark: str | None = None


class ReviewRun(BaseModel):
    """一次审查运行（P1-R2-4：分析/发布状态分离）。"""

    run_id: str
    external_ref: str | None = Field(default=None, description="仅 github_pr 来源")
    base_sha: str = ""
    head_sha: str = ""
    strategy: ReviewStrategyName = ReviewStrategyName.SINGLE_PASS
    status: ReviewRunStatus = ReviewRunStatus.PENDING
    publish_status: str | None = Field(default=None, description="发布状态，与分析状态独立")
    warnings: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    stages: list[StageResult] = Field(default_factory=list)
    coverage: CoverageManifest = Field(default_factory=CoverageManifest)
    config_snapshot_hash: str | None = None

    def finish(self, status: ReviewRunStatus) -> None:
        self.status = status
        self.finished_at = utcnow()


class FeedbackMemory(BaseModel):
    """仓库长期反馈记忆（V2，条件化匹配，P0-R2-1）。"""

    repo: str
    kind: FeedbackKind
    scope: str | None = None
    path: str | None = None
    symbol: str | None = None
    category: str | None = None
    rule_key: str | None = None
    pattern: str | None = None
    cross_run_match_key: str | None = None
    rationale: str = ""
    active: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    revoked_at: datetime | None = None

    def revoke(self) -> None:
        self.active = False
        self.revoked_at = utcnow()
