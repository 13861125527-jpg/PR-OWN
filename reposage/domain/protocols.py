"""Provider 抽象协议（domain/protocols.py）。

方向：domain 定义抽象，providers/ 实现并依赖 domain；domain 不得依赖具体 Provider 实现。
V1 使用的边界一律使用具体类型；V3 tool_loop 使用 domain 层 AgentSessionView。
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from .enums import PublishPlanStatus, RevokeFeedbackResult
from .finding import Finding, FindingCandidate
from .models import (
    AgentMessage,
    AgentToolRequest,
    ChangeRequest,
    CoverageManifest,
    GlobalBudget,
    ModelUsage,
    ReviewContext,
    ToolCall,
    ToolResult,
)
from .run import (
    DeleteCommentRequest,
    FeedbackMemory,
    GateDecision,
    PublishCommentResult,
    PublishOperation,
    PublishPlan,
    ReviewRun,
    ReviewTask,
)


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

    async def delete_comment(self, request: DeleteCommentRequest) -> bool:
        """删除已发布评论（supersede 清理，P0-R2-2）。返回是否成功。

        安全契约（V1-e 返工 P1）：Provider 删除前必须读取远程评论并校验
        - remote_comment_id 属于 request.pr_identity 对应的 PR；
        - 评论 body 含 request.expected_marker（本机器人 marker）。
        任一不匹配则拒绝删除（返回 False），不得凭裸 id 全局删除他人评论。
        """
        ...


class GitSnapshotProvider(Protocol):
    """只读 head 快照能力（V2-B L3）。与 GitProvider 解耦，避免非 L3 桩承担无关方法。"""

    async def get_blob(self, sha: str, path: str) -> str | None:
        """返回某 SHA 下文件正文；缺失、二进制或不可读返回 None。"""
        ...

    async def list_paths(self, sha: str) -> list[str]:
        """返回某 SHA 下全部文件路径。"""
        ...


def as_snapshot_provider(obj: object) -> GitSnapshotProvider | None:
    """两项能力都存在才视为 Snapshot Provider。"""
    get_blob = getattr(obj, "get_blob", None)
    list_paths = getattr(obj, "list_paths", None)
    if callable(get_blob) and callable(list_paths):
        return obj  # type: ignore[return-value]
    return None


class AgentSessionView(Protocol):
    """Provider 只读会话视图；不得暴露可变 candidates/calls。"""

    session_id: str
    mode: str
    messages: tuple[AgentMessage, ...]

    def remaining_rounds(self) -> int: ...

    def budget_summary(self) -> dict[str, Any]: ...


class ModelResponse:
    """LLM 响应载体（结构化输出或工具动作）。"""

    def __init__(
        self,
        text: str = "",
        data: dict[str, Any] | None = None,
        usage: ModelUsage | None = None,
        action: str | None = None,
        provider_message_id: str | None = None,
        tool_requests: list[AgentToolRequest] | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        self.text = text
        self.data = data
        self.usage = usage
        self.action = action  # None | tool_call | submit_finding | finish_review
        self.provider_message_id = provider_message_id
        self.tool_requests = tool_requests or []
        self.reasoning_content = reasoning_content


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
        session: AgentSessionView,
        tools: list[dict[str, Any]],
        *,
        budget: GlobalBudget,
        protocol: str = "native",
    ) -> ModelResponse:
        """V3 工具循环（原生 tool calling 或受限 Action JSON 协议）。"""
        ...


class Storage(Protocol):
    """存储抽象（async；SQLite 实现内部用 to_thread 避免阻塞事件循环）。"""

    async def record_run(self, run: ReviewRun) -> None: ...

    async def record_findings(self, findings: list[Finding]) -> None: ...

    async def record_usage(self, run_id: str, usage: ModelUsage) -> None: ...

    async def record_tasks(self, tasks: list[ReviewTask]) -> None:
        """落库文件/角色/agent 任务记录（V1-f：成本/Token/耗时指标）。"""
        ...

    async def record_coverage(self, run_id: str, coverage: CoverageManifest) -> None:
        """落库覆盖清单（V1-f：CoverageItem 全指标）。"""
        ...

    async def record_gate_decisions(self, run_id: str, decisions: list[GateDecision]) -> None:
        """落库门控决策（V2-A；含 miss；不存源码）。"""
        ...

    # ---- V1-e：发布（Saga/Outbox） ----

    async def record_publish_plan(self, plan: PublishPlan) -> None: ...

    async def record_comment_results(
        self, plan_id: str, results: dict[str, PublishCommentResult], lease_owner: str
    ) -> None: ...

    async def record_operation(self, op: PublishOperation, lease_owner: str) -> None: ...

    async def update_plan_status(
        self, plan_id: str, status: PublishPlanStatus, lease_owner: str
    ) -> None:
        """fencing：只有持有当前租约的 owner 才能推进 plan 状态（V1-e 五轮）。

        WHERE plan_id AND lease_owner = ?；rowcount==0 时抛 LeaseLostError。
        """
        ...

    async def assert_lease(self, plan_id: str, lease_owner: str) -> None:
        """验证租约仍归 owner（V1-e 五轮 fencing）：不匹配抛 LeaseLostError。

        供 Publisher 在远端调用前与关键写操作前检查，确保租约丢失后立即停止。
        """
        ...

    async def load_publish_plan(self, plan_id: str) -> PublishPlan | None: ...

    async def load_recoverable_plans(self, run_id: str) -> list[PublishPlan]: ...

    async def update_finding_status(
        self, finding_occurrence_id: str, status: str, plan_id: str, lease_owner: str
    ) -> None: ...

    # ---- V1-e 四轮：崩溃安全（published + outbox 同事务）与并发租约 ----

    async def record_plan_published_with_cleanup(
        self, plan_id: str, committed_watermark: str, op: PublishOperation, lease_owner: str
    ) -> None:
        """同事务持久化「plan → published + committed_watermark + cleanup op PENDING」。

        带 fencing（V1-e 五轮 P0）：先 UPDATE plan（WHERE lease_owner），rowcount==0 则
        整个事务回滚（禁止写入 operation）并抛 LeaseLostError。
        """
        ...

    async def finish_cleanup(self, plan_id: str, *, success: bool, lease_owner: str) -> None:
        """收敛 plan 状态：success → completed，否则 → cleanup_pending。带 fencing。

        op 状态由 _run_cleanup / fail-closed 分支各自正确落库，此处只写 plan 状态，
        避免多 op 场景下用整体结果覆盖单个 op 的真实状态（V1-e 四轮复验 should-fix）。
        """
        ...

    async def claim_plan(self, plan_id: str, lease_owner: str, lease_until: str, now: str) -> bool:
        """原子租约 claim：只有 claim 成功的 Worker 才能执行远程副作用。

        CAS：WHERE status IN (可恢复状态) AND (lease_until IS NULL OR lease_until < now)，
        检查受影响行数。now 是当前时间，不得用新到期时间代替（V1-e 五轮 P0）。
        """
        ...

    async def release_lease(self, plan_id: str, lease_owner: str) -> None:
        """释放租约：正常结束（partial/completed/cleanup_pending）后清空 lease。

        带 fencing：只有当前 owner 能释放自己的租约，不误释放他人已接管的租约。
        """
        ...

    async def retire_remote_id(
        self, plan_id: str, pr_identity: str, marker: str, stale_remote_id: int, lease_owner: str
    ) -> None:
        """外部删除后收敛：Provider 返回新 ID 时，把同 PR 同 marker 的旧 active 映射置 superseded。

        带 fencing（V1-e 五轮）：校验 plan 租约，丢失则抛 LeaseLostError。
        """
        ...

    async def mark_plan_obsolete(self, plan_id: str) -> None:
        """旧 head 的 partial/publishing/prepared plan 被新 head 取代时标记 OBSOLETE。"""
        ...

    # ---- V1-e 返工：跨 run 幂等查询 ----

    async def load_published_remote_ids(self, pr_identity: str) -> dict[str, int]:
        """按 PR 身份查 marker→remote_comment_id 映射（已发布评论，跨 plan）。"""
        ...

    async def load_superseded_comment_ids(
        self, pr_identity: str, exclude_plan_id: str
    ) -> list[DeleteCommentRequest]:
        """查同 PR 上一成功计划的已发布评论（供 supersede 清理）。"""
        ...

    async def load_latest_plan(self, pr_identity: str) -> PublishPlan | None:
        """按 PR 身份查最近发布计划（任意状态）。"""
        ...

    async def load_latest_recoverable_publish_plan(self, pr_identity: str) -> PublishPlan | None:
        """按 PR 身份查最近可恢复的正式发布计划（V1-e 三轮 P1）。

        过滤 mode='publish' 且状态为 prepared/publishing/partial/cleanup_pending，
        按最新顺序取一条，避免 dry-run 的 prepared plan 遮挡旧 partial/cleanup_pending。
        """
        ...

    async def mark_comments_superseded(
        self, plan_id: str, remote_comment_ids: list[int], lease_owner: str
    ) -> None:
        """清理成功后把对应旧评论状态收敛为 superseded（V1-e 三轮 P0）。带 fencing。"""
        ...

    # ---- V2-E：反馈记忆 + 增量 watermark ----

    async def record_feedback(self, memory: FeedbackMemory) -> int:
        """插入一条反馈记忆，返回 id。"""
        ...

    async def list_feedback(
        self, repo: str | None, *, include_revoked: bool
    ) -> list[FeedbackMemory]: ...

    async def revoke_feedback(self, feedback_id: int, *, repo: str) -> RevokeFeedbackResult:
        """按 id 软删除；必须属于 repo。已撤销同 repo 返回 already_revoked（幂等）。"""
        ...

    async def load_active_feedback(self, repo: str) -> list[FeedbackMemory]: ...

    async def get_finding(self, finding_occurrence_id: str) -> Finding | None: ...

    async def load_committed_watermark(self, pr_identity: str) -> str | None:
        """最新成功发布的 committed_watermark（published/cleanup_pending/completed）。"""
        ...

    async def record_tool_invocation(self, call: ToolCall, result: ToolResult) -> None:
        """同事务写入 tool_calls + tool_results；同值幂等，冲突拒绝。"""
        ...


__all__ = [
    "GitProvider",
    "GitSnapshotProvider",
    "as_snapshot_provider",
    "AgentSessionView",
    "LLMProvider",
    "ModelResponse",
    "Storage",
    "GlobalBudget",
    "ReviewContext",
]
