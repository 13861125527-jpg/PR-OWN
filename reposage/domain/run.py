"""运行、覆盖与发布计划模型（domain/run.py）。

契约来源：05 §2（ReviewRun/StageResult/CoverageManifest/PublishPlan/CommentPlan/PublishOperation）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .enums import (
    CommentKind,
    CommentStatus,
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
from .models import CoverageItem, CoverageManifest, ModelUsage, utcnow


class LeaseLostError(RuntimeError):
    """租约已被其他 Worker 接管（V1-e 五轮 fencing）。

    持有租约的 Worker 在租约过期后被接管时，其后续状态写/远端调用必须立即停止，
    避免旧 Worker 覆盖新 Worker 的状态或产生额外远端副作用。
    """


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
    """角色注册表条目（V2-A，07 §2 / 18 §3.1）。"""

    id: str = Field(description="如 security")
    prompt_id: str = Field(default="", description="prompts/roles/<id>.md 键")
    gate_id: str = Field(default="always", description="纯函数键；always = 只过语言门")
    model_requirement: str = Field(default="general", description="路由能力档")
    enabled: bool = True  # builtin 默认；有效启用见 RoleRegistry 公式
    implemented: bool = True  # False = 占位 ID，禁止配置启用
    required: bool = False  # optional 角色失败不放大为 PARTIAL（P1-R2-4）
    budget_weight: float = Field(default=1.0, gt=0.0)
    languages: list[str] = Field(default_factory=list, description="空 = 跟随 review.languages")


class GateDecision(BaseModel):
    """一次 (file, role) 门控结果（可落库；matched_features 只含规则 ID）。"""

    role_id: str
    file_path: str
    enabled: bool
    matched_features: list[str] = Field(default_factory=list)
    reason: str = Field(description="always | gate_hit | gate_miss | lang_miss | no_added_lines")
    gate_version: str = ""


class StrategyHealth(BaseModel):
    """Strategy 向 Service 提供的通用归并契约（V2-A Blocking-4）。"""

    required_failed: bool = False
    required_failure_count: int = 0
    optional_failure_count: int = 0
    coverage_complete: bool = True


class SourceRunResult(BaseModel):
    """Strategy 产出（与 CandidateFinding 一起返回，03 §5）。"""

    strategy: ReviewStrategyName
    tasks: list[ReviewTask] = Field(default_factory=list)
    usages: list[ModelUsage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    health: StrategyHealth = Field(default_factory=StrategyHealth)
    gate_decisions: list[GateDecision] = Field(default_factory=list)
    coverage_items: list[CoverageItem] = Field(default_factory=list)


class CommentPlan(BaseModel):
    """单条评论计划（P0-R2-2：required 标注）。"""

    comment_id: str
    required: bool = True
    finding_occurrence_id: str | None = None
    fingerprint: str | None = None
    # 跨 run 幂等键（V1-e 返工 P0）：= cross_run_match_key（容忍代码移动，不含 head_sha）；
    # summary 用固定槽位 "summary"。marker 由 pr_identity + stable_key + kind 构成，
    # 不依赖随机 plan_id，保证同 PR 二次运行零增量。
    stable_key: str = ""
    kind: CommentKind = CommentKind.INLINE
    path: str | None = None
    line: int | None = None
    body: str = ""
    marker: str = ""
    remote_comment_id: int | None = None
    status: CommentStatus = CommentStatus.PREPARED


class PublishCommentResult(BaseModel):
    """单条评论发布结果（Fake 与真实 Provider 共用契约）。"""

    comment_id: str
    status: CommentStatus = CommentStatus.PUBLISHED
    remote_comment_id: int | None = None
    error: str | None = None


class DeleteCommentRequest(BaseModel):
    """结构化删除请求（V1-e 返工 P1）：Provider 删除前必须逐项校验归属。"""

    remote_comment_id: int
    pr_identity: str = Field(description="目标 PR 身份（repo#PR number）")
    expected_marker: str = Field(description="预期机器人 marker；不匹配则拒绝删除")


class PublishOperation(BaseModel):
    """发布独立任务（supersede 清理等，P0-R2-2）。"""

    op_id: str
    plan_id: str = Field(description="所属发布计划")
    kind: PublishOperationKind = PublishOperationKind.SUPERSEDE_CLEANUP
    status: PublishOperationStatus = PublishOperationStatus.PENDING
    detail: str | None = None


class PublishPlan(BaseModel):
    """发布计划（Saga/Outbox 状态机，07 §7）。"""

    plan_id: str
    run_id: str
    pr_identity: str = Field(default="", description="稳定 PR 身份（repo#PR number；幂等键，V1-e 返工 P0）")
    mode: str = Field(default="dry_run", description="dry_run | publish")
    status: PublishPlanStatus = PublishPlanStatus.PREPARED
    summary_comment: str | None = None
    comments: list[CommentPlan] = Field(default_factory=list)
    operations: list[PublishOperation] = Field(default_factory=list)
    # 候选值与已推进值分离（V1-e 四轮 P1）：target_head_sha 在准备阶段即可设置，
    # committed_watermark 仅在必要评论全部发布成功后才写入，二者语义不再混同。
    target_head_sha: str | None = Field(default=None, description="本计划目标发布的 head SHA")
    committed_watermark: str | None = Field(default=None, description="必要评论全部发布成功后推进的 watermark")
    allow_supersede_cleanup: bool = Field(
        default=True,
        description="False 时不删除未出现在本 plan 的旧评论（增量审查，V2-E）",
    )
    warnings: list[str] = Field(default_factory=list)


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

    id: int | None = None
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
