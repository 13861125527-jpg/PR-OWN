"""发布器（publishing/publisher.py，V1-e，07 §7 / 10 §7 / 16 P0-R2-2）。

Saga/Outbox 状态机：从定稿 Finding 构建 PublishPlan（summary + inline/body 分类 +
marker）→ dry-run（只生成不发布）或正式发布（prepared → publishing →
published/partial → cleanup_pending → completed，含 supersede 清理与 watermark
推进）→ 重跑恢复。

契约要点：
- 必要评论：summary 必须；V1 中所有 accepted inline/body 评论均为必须项；
- 幂等三组合：DB remote_comment_id 映射优先 → marker 兜底 → 查询后 update/create；
- watermark：plan=published 即推进 last_reviewed_sha；supersede 清理不阻塞 watermark；
- 分析状态（ReviewRun.status）与发布状态（publish_status）分离，本模块只写 publish 侧；
- Finding 状态推进 published / publish_failed（Saga 状态机是程序推进者，05 §3）。
"""

from __future__ import annotations

import json
import re
import uuid

from reposage.domain.enums import (
    CommentKind,
    CommentStatus,
    FindingStatus,
    PublishOperationKind,
    PublishOperationStatus,
    PublishPlanStatus,
    Severity,
)
from reposage.domain.finding import Finding
from reposage.domain.protocols import GitProvider, Storage
from reposage.domain.run import (
    CommentPlan,
    DeleteCommentRequest,
    LeaseLostError,
    PublishCommentResult,
    PublishOperation,
    PublishPlan,
    ReviewRun,
)
from reposage.publishing.identity import pr_identity_for

_SEVERITY_RANK = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}


def _redact(text: str) -> str:
    """评论文本脱敏（10 §7 / observability.redact_secrets）：剥离凭证片段。

    模型产出的 explanation/suggestion/title 可能复述源码中的密钥，发布到 GitHub 后
    公开可见（公开仓库全网可见），必须发布前统一清洗。覆盖常见凭证前缀 + key=value
    高熵值通用掩码；不追求与专用 secret 扫描器等价，但覆盖公开场景的主要泄漏形态。
    """
    cleaned = re.sub(
        r"(?i)(sk-[a-z0-9_-]{4,}|bearer\s+[a-z0-9._-]{4,}|"
        r"gh[pousr]_[a-z0-9]{8,}|github_pat_[a-z0-9_]{8,}|"
        r"akia[a-z0-9]{12,}|xox[baprs]-[a-z0-9-]{8,}|ai[za][a-z0-9_-]{8,})",
        "<redacted>",
        text,
    )
    # key=value 形式的高熵值（api_key/password/token/secret/connection string）
    cleaned = re.sub(
        r"(?i)(api[_-]?key|password|passwd|token|secret)\s*[=:]\s*[\"']?[^\s\"',;]{8,}[\"']?",
        r"\1=<redacted>",
        cleaned,
    )
    # PEM 私钥块
    cleaned = re.sub(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        "<redacted-private-key>",
        cleaned,
        flags=re.DOTALL,
    )
    # 数据库连接串中的 user:pass@
    cleaned = re.sub(r"(?i)(postgres(?:ql)?|mysql|mongodb(\+srv)?)://[^:/\s]+:[^@/\s]+@", r"\1://<redacted>@", cleaned)
    return cleaned


class Publisher:
    """发布器：构建计划 + Saga 发布 + 重跑恢复（V1-e）。"""

    def __init__(
        self,
        git: GitProvider,
        storage: Storage,
        *,
        repo: str = "",
        body_threshold_severity: str = "medium",
    ) -> None:
        self.git = git
        self.storage = storage
        self.repo = repo
        self.body_threshold = _SEVERITY_RANK[Severity(body_threshold_severity)]

    # ---- 计划构建 ----

    def build_plan(
        self,
        run: ReviewRun,
        findings: list[Finding],
        *,
        mode: str = "dry_run",
        allow_supersede_cleanup: bool = True,
    ) -> PublishPlan:
        """从定稿 Finding 构建发布计划。

        分类（07 §7）：
        - 行内（inline）：ACCEPTED 且 severity ≥ body_threshold（有 canonical 行）；
        - 正文（body）：BODY_ONLY（位置越界/跨文件，但问题成立）；
        - 仅摘要：ACCEPTED 但 severity < body_threshold 的低严重度项不进评论流；
        - summary：始终为第一条必要评论（kind=SUMMARY）。

        幂等（V1-e 返工 P0）：marker 由稳定 PR 身份 + cross_run_match_key + kind 构成，
        不依赖随机 plan_id——同 PR 二次运行（新 run/新 plan）marker 不变，零增量。
        """
        plan_id = f"plan-{uuid.uuid4().hex[:12]}"
        pr_identity = self._pr_identity(run)
        plan = PublishPlan(
            plan_id=plan_id,
            run_id=run.run_id,
            pr_identity=pr_identity,
            mode=mode,
            status=PublishPlanStatus.PREPARED,
            target_head_sha=run.head_sha,  # 候选值（V1-e 四轮 P1）
            committed_watermark=None,  # 未推进，仅在发布成功后写入
            allow_supersede_cleanup=allow_supersede_cleanup,
        )

        inline_or_body: list[Finding] = []
        summary_only: list[Finding] = []
        for f in findings:
            is_accepted_above = (
                f.status is FindingStatus.ACCEPTED
                and _SEVERITY_RANK[f.severity] >= self.body_threshold
            )
            if is_accepted_above or f.status is FindingStatus.BODY_ONLY:
                inline_or_body.append(f)
            elif f.status is FindingStatus.ACCEPTED:
                summary_only.append(f)

        # summary：稳定槽位 "summary"
        summary_marker = self._marker(pr_identity, "summary", CommentKind.SUMMARY)
        summary_body = self._summary_text(findings, summary_only, mode) + "\n" + summary_marker
        plan.comments.append(
            CommentPlan(
                comment_id="summary",
                required=True,
                stable_key="summary",
                kind=CommentKind.SUMMARY,
                body=summary_body,
                marker=summary_marker,
            )
        )
        plan.summary_comment = summary_body

        for f in inline_or_body:
            kind = (
                CommentKind.INLINE
                if f.status is FindingStatus.ACCEPTED and f.canonical_start_line is not None
                else CommentKind.BODY
            )
            comment_id = f"c-{len(plan.comments)}"
            stable_key = f.cross_run_match_key or f.fingerprint or f"{f.category.value}:{f.canonical_path or ''}:{f.title}"
            marker = self._marker(pr_identity, stable_key, kind)
            plan.comments.append(
                CommentPlan(
                    comment_id=comment_id,
                    required=True,  # V1：所有 inline/body 均为必须项
                    finding_occurrence_id=f.finding_occurrence_id,
                    fingerprint=f.fingerprint,
                    stable_key=stable_key,
                    kind=kind,
                    path=f.canonical_path,
                    line=f.canonical_start_line,
                    body=self._comment_body(f, marker),
                    marker=marker,
                )
            )
        return plan

    def _pr_identity(self, run: ReviewRun) -> str:
        """稳定 PR 身份（V1-e 返工 P0）：repo + external_ref（PR number）。

        local_range 无 external_ref 时退化为 head_sha（本地 dry-run 不参与跨 run 幂等）。
        """
        return pr_identity_for(run, self.repo)

    def _marker(self, pr_identity: str, stable_key: str, kind: CommentKind) -> str:
        """稳定 marker：<!-- reposage:{pr_identity}:{stable_key}:{kind} -->。"""
        return f"<!-- reposage:{pr_identity}:{stable_key}:{kind.value} -->"

    def _comment_body(self, f: Finding, marker: str) -> str:
        """单条评论文本（脱敏：不含凭证，仅问题描述 + 建议）。"""
        parts = [f"[{f.severity.value}][{f.category.value}] {_redact(f.title)}"]
        if f.explanation:
            parts.append(_redact(f.explanation))
        if f.suggestion:
            parts.append(f"建议：{_redact(f.suggestion)}")
        parts.append(marker)
        return "\n".join(parts)

    def _summary_text(self, findings: list[Finding], summary_only: list[Finding], mode: str) -> str:
        """摘要评论文本。"""
        accepted = [f for f in findings if f.status is FindingStatus.ACCEPTED]
        body_only = [f for f in findings if f.status is FindingStatus.BODY_ONLY]
        by_severity: dict[str, int] = {}
        for f in accepted:
            by_severity[f.severity.value] = by_severity.get(f.severity.value, 0) + 1
        lines = [f"RepoSage 审查摘要（{'dry-run' if mode == 'dry_run' else '正式'}）："]
        lines.append(
            f"共 {len(accepted)} 条 accepted、{len(body_only)} 条 body_only。"
        )
        if by_severity:
            lines.append(
                "按严重度：" + "、".join(f"{k} {v}" for k, v in sorted(by_severity.items()))
            )
        if summary_only:
            lines.append(f"低严重度（仅摘要，未进评论流）：{len(summary_only)} 条。")
        return "\n".join(lines)

    # ---- Saga 发布 ----

    async def publish(self, plan: PublishPlan) -> PublishPlan:
        """执行 Saga 发布（07 §7 / V1-e 四轮 P0/P1）。

        - 幂等：发布前按 PR 身份查历史 remote_comment_id，预填到 plan.comments，
          Provider 复用（跨 run/重启零增量）；
        - 崩溃安全：published + cleanup outbox 同事务落库；远程删除前 outbox 已可靠持久化；
        - 并发租约：claim 成功才执行远程副作用（两个 Worker 争抢同一 plan 只一个生效）；
        - 成功条件：必要评论 status is PUBLISHED；缺失/FAILED/PREPARED 都视为失败。
        """
        # 幂等预填：跨 run 稳定 marker → 历史 remote_id（记录 prefill，供外部删除收敛）
        if plan.pr_identity:
            history = await self.storage.load_published_remote_ids(plan.pr_identity)
            for comment in plan.comments:
                if comment.remote_comment_id is None and comment.marker in history:
                    comment.remote_comment_id = history[comment.marker]

        await self.storage.record_publish_plan(plan)

        # 并发租约 claim（V1-e 四轮 P1 / 五轮 P0）：只有抢占成功的 Worker 执行远程副作用。
        # 放在 cleanup 恢复判断之前，保证恢复清理路径也受 claim 保护（复验 should-fix）。
        # now 用当前时间比较 lease 过期，不得用新到期时间代替（五轮 P0 阻断修复）。
        lease_owner = f"worker-{uuid.uuid4().hex[:8]}"
        lease_until = self._lease_until_str()
        now = self._now_str()
        if not await self.storage.claim_plan(plan.plan_id, lease_owner, lease_until, now):
            # 其他 Worker 正在处理，返回当前 DB 状态，避免重复远程副作用
            current = await self.storage.load_publish_plan(plan.plan_id)
            return current or plan

        # claim 成功：无论正常/异常结束都必须释放租约（V1-e 五轮 P0），否则立即重试会
        # 被迫等满租期。try/finally 保证 partial/completed/cleanup_pending/异常都释放。
        try:
            return await self._publish_owned(plan, lease_owner)
        except LeaseLostError:
            # 租约已被接管：放弃本次发布，返回当前 DB 状态（V1-e 五轮 fencing）
            current = await self.storage.load_publish_plan(plan.plan_id)
            return current or plan
        finally:
            await self.storage.release_lease(plan.plan_id, lease_owner)

    async def _publish_owned(self, plan: PublishPlan, lease_owner: str) -> PublishPlan:
        """claim 成功后执行发布/恢复主流程（持有租约，V1-e 五轮 P0）。"""
        # 重跑恢复分支：cleanup_pending 或 published+未完成清理 只重试清理，不重新发布
        if self.needs_cleanup_resume(plan):
            return await self._resume_cleanup(plan, lease_owner)

        await self.storage.update_plan_status(
            plan.plan_id, PublishPlanStatus.PUBLISHING, lease_owner
        )

        # 远端调用前校验租约（V1-e 五轮 fencing）：租约丢失则立即停止，不产生远端副作用
        await self.storage.assert_lease(plan.plan_id, lease_owner)
        results = await self.git.publish_comments(plan)
        # 回写逐条状态（缺失结果的评论保持 PREPARED → 视为失败，V1-e 返工 P0）。
        # 缺失结果也构造 FAILED 一并回写，持久化语义一致（审查 should-fix）。
        for comment in plan.comments:
            prefill_id = comment.remote_comment_id  # 发布前预填的 remote ID
            r = results.get(comment.comment_id)
            if r is None:
                comment.status = CommentStatus.FAILED  # 缺失结果 → 视为失败
                results[comment.comment_id] = PublishCommentResult(
                    comment_id=comment.comment_id,
                    status=CommentStatus.FAILED,
                    error="provider missing result",
                )
                continue
            comment.status = r.status
            comment.remote_comment_id = r.remote_comment_id
            # 外部删除收敛（V1-e 四轮 P0）：Provider 返回的 ID 与预填不同，
            # 说明预填的旧 ID 已失效（用户手工删除），收敛旧 active 映射。
            if (
                prefill_id is not None
                and r.remote_comment_id is not None
                and r.remote_comment_id != prefill_id
                and comment.marker
            ):
                await self.storage.retire_remote_id(
                    plan.plan_id, plan.pr_identity, comment.marker, prefill_id, lease_owner
                )
        await self.storage.record_comment_results(plan.plan_id, results, lease_owner)

        # P2（V1-e 三轮）：Provider 返回了计划之外的 comment_id 视为异常，不得无证据地
        # completed。额外 ID 记入 warning，并把计划降级为 partial（避免假成功）。
        planned_ids = {c.comment_id for c in plan.comments}
        extra_ids = sorted(set(results.keys()) - planned_ids)
        if extra_ids:
            plan.warnings.append(
                f"provider returned unexpected comment ids: {extra_ids}"
            )
            await self.storage.update_plan_status(
                plan.plan_id, PublishPlanStatus.PARTIAL, lease_owner
            )
            plan.status = PublishPlanStatus.PARTIAL
            await self._transition_findings(plan, lease_owner)
            return plan

        # 成功条件：必要评论必须 PUBLISHED（缺失/PREPARED/FAILED 均失败）
        required_not_published = [
            c
            for c in plan.comments
            if c.required and c.status is not CommentStatus.PUBLISHED
        ]
        if required_not_published:
            await self.storage.update_plan_status(
                plan.plan_id, PublishPlanStatus.PARTIAL, lease_owner
            )
            plan.status = PublishPlanStatus.PARTIAL
            await self._transition_findings(plan, lease_owner)
            return plan

        watermark = plan.target_head_sha
        assert watermark is not None  # build_plan 必设 head_sha（候选值）

        # supersede 清理：同 PR 上一成功计划的评论（自动发现，V1-e 返工 P1）。
        # 关键：排除当前 plan 复用过的 marker（跨 run 幂等使新旧 marker 相同 → 同 remote_id），
        # 否则会把刚复用（当前生效）的评论也删掉（审查 blocking）。
        current_markers = {c.marker for c in plan.comments if c.marker}
        if plan.allow_supersede_cleanup:
            superseded = [
                t
                for t in await self.storage.load_superseded_comment_ids(
                    plan.pr_identity, exclude_plan_id=plan.plan_id
                )
                if t.expected_marker not in current_markers
            ]
        else:
            superseded = []
        op = PublishOperation(
            op_id=f"op-{uuid.uuid4().hex[:12]}",
            plan_id=plan.plan_id,
            kind=PublishOperationKind.SUPERSEDE_CLEANUP,
            status=PublishOperationStatus.PENDING,
            detail=json.dumps([t.model_dump() for t in superseded], ensure_ascii=False),
        )
        plan.operations.append(op)

        # 崩溃安全（V1-e 四轮 P0）：published + committed_watermark + outbox op PENDING
        # 在同一事务落库，远程删除前 outbox 已可靠持久化。之后任何一步崩溃都能恢复。
        await self.storage.record_plan_published_with_cleanup(
            plan.plan_id, watermark, op, lease_owner
        )
        plan.status = PublishPlanStatus.PUBLISHED
        plan.committed_watermark = watermark
        await self._transition_findings(plan, lease_owner)

        # 执行清理（此时 outbox 已落库，崩溃可恢复）
        cleanup_ok = await self._run_cleanup(op, superseded, lease_owner)
        await self.storage.finish_cleanup(plan.plan_id, success=cleanup_ok, lease_owner=lease_owner)
        plan.status = (
            PublishPlanStatus.COMPLETED if cleanup_ok else PublishPlanStatus.CLEANUP_PENDING
        )
        return plan

    def needs_cleanup_resume(self, plan: PublishPlan) -> bool:
        """判断是否进入 cleanup 恢复分支（不重新发布评论）。公开给 service 复用。"""
        if plan.status is PublishPlanStatus.CLEANUP_PENDING:
            return True
        if plan.status is PublishPlanStatus.PUBLISHED:
            return any(
                op.kind is PublishOperationKind.SUPERSEDE_CLEANUP
                and op.status
                in (
                    PublishOperationStatus.PENDING,
                    PublishOperationStatus.RUNNING,
                    PublishOperationStatus.FAILED,
                )
                for op in plan.operations
            )
        return False

    @staticmethod
    def _lease_until_str() -> str:
        """租约过期时间（默认 600s，明确大于单次远程操作最大超时，V1-e 五轮 P0）。"""
        from datetime import timedelta

        from reposage.domain.models import utcnow

        return (utcnow() + timedelta(seconds=600)).isoformat()

    @staticmethod
    def _now_str() -> str:
        """当前时间（用于 claim 过期比较，不得用新到期时间代替）。"""
        from reposage.domain.models import utcnow

        return utcnow().isoformat()

    async def _run_cleanup(
        self, op: PublishOperation, targets: list[DeleteCommentRequest], lease_owner: str
    ) -> bool:
        """supersede 清理：删除旧评论（独立任务，失败不阻塞 watermark）。

        待删目标以 JSON 持久化到 op.detail（结构化，非逗号字符串），恢复时可重试。
        删除成功（含"远程不存在"）后立即把对应旧评论收敛为 superseded（V1-e 三轮 P0），
        避免 DB 状态与远端分叉、后续跨 run 幂等复用失效 remote ID。
        任何 delete_comment 异常都捕获为失败（V1-e 四轮 P0），不遗留 running。
        """
        op.status = PublishOperationStatus.RUNNING
        op.detail = json.dumps([t.model_dump() for t in targets], ensure_ascii=False)
        await self.storage.record_operation(op, lease_owner)
        ok = True
        superseded_ids: list[int] = []
        for t in targets:
            # 远端删除前校验租约（V1-e 五轮 fencing）：丢失则立即停止
            await self.storage.assert_lease(op.plan_id, lease_owner)
            try:
                deleted = await self.git.delete_comment(t)
            except Exception:  # noqa: BLE001 —— 远程删除异常 → 视为失败，可恢复重试
                ok = False
                break
            if deleted:
                superseded_ids.append(t.remote_comment_id)
            else:
                ok = False
                break
        # 删除成功的（含远端本就不存在）都要收敛 DB 状态，与远端保持一致
        if superseded_ids:
            await self.storage.mark_comments_superseded(op.plan_id, superseded_ids, lease_owner)
        op.status = PublishOperationStatus.DONE if ok else PublishOperationStatus.FAILED
        await self.storage.record_operation(op, lease_owner)
        return ok

    async def _resume_cleanup(self, plan: PublishPlan, lease_owner: str) -> PublishPlan:
        """重跑恢复：cleanup_pending / published+未完成清理 只重试清理，不重新发布。

        fail-closed（V1-e 四轮 P1）：cleanup payload 损坏或缺失时不得声称清理完成，
        op=failed + plan=cleanup_pending，并记录脱敏错误。
        补推进 finding（V1-e 四轮复验 should-fix）：崩溃在 published 落库后、_transition_findings
        前时，恢复路径必须补推进已发布评论对应的 finding 状态（_transition_findings 幂等）。
        """
        await self._transition_findings(plan, lease_owner)
        pending_ops = [
            op
            for op in plan.operations
            if op.kind is PublishOperationKind.SUPERSEDE_CLEANUP
            and op.status in (PublishOperationStatus.PENDING, PublishOperationStatus.FAILED, PublishOperationStatus.RUNNING)
        ]
        if not pending_ops:
            # 无可重试任务 → 视为清理完成
            await self.storage.update_plan_status(
                plan.plan_id, PublishPlanStatus.COMPLETED, lease_owner
            )
            plan.status = PublishPlanStatus.COMPLETED
            return plan
        all_ok = True
        for op in pending_ops:
            targets, err = self._parse_cleanup_targets(op.detail)
            if err is not None:
                # fail-closed：payload 损坏 → op=failed + plan=cleanup_pending，不声称完成
                op.status = PublishOperationStatus.FAILED
                op.detail = op.detail  # 保留原 detail 供诊断
                await self.storage.record_operation(op, lease_owner)
                all_ok = False
                continue
            ok = await self._run_cleanup(op, targets, lease_owner)
            if not ok:
                all_ok = False
        await self.storage.finish_cleanup(plan.plan_id, success=all_ok, lease_owner=lease_owner)
        plan.status = (
            PublishPlanStatus.COMPLETED if all_ok else PublishPlanStatus.CLEANUP_PENDING
        )
        return plan

    def _parse_cleanup_targets(
        self, detail: str | None
    ) -> tuple[list[DeleteCommentRequest], str | None]:
        """从 op.detail（JSON）解析待删目标（恢复用）。返回 (targets, error)。

        fail-closed（V1-e 四轮 P1）：detail 为空/非法 JSON/字段校验失败时返回 error，
        调用方必须把 op 标 failed + plan cleanup_pending，不得声称清理完成。
        """
        if not detail:
            return [], "cleanup payload missing"
        try:
            raw = json.loads(detail)
        except json.JSONDecodeError:
            return [], "cleanup payload is not valid JSON"
        if not isinstance(raw, list):
            return [], "cleanup payload is not a list"
        try:
            targets = [DeleteCommentRequest.model_validate(item) for item in raw]
        except Exception:  # noqa: BLE001 —— 字段校验失败同样 fail-closed
            return [], "cleanup payload field validation failed"
        return targets, None

    async def _transition_findings(self, plan: PublishPlan, lease_owner: str) -> None:
        """推进 Finding 状态（05 §3 Saga 状态机推进）。

        逐条按 comment.status 判定：published → finding published；
        failed → publish_failed（含 partial 时已成功的评论仍标 published）。
        """
        for comment in plan.comments:
            if comment.kind is CommentKind.SUMMARY or comment.finding_occurrence_id is None:
                continue
            if comment.status is CommentStatus.PUBLISHED:
                await self.storage.update_finding_status(
                    comment.finding_occurrence_id,
                    FindingStatus.PUBLISHED.value,
                    plan.plan_id,
                    lease_owner,
                )
            elif comment.status is CommentStatus.FAILED:
                await self.storage.update_finding_status(
                    comment.finding_occurrence_id,
                    FindingStatus.PUBLISH_FAILED.value,
                    plan.plan_id,
                    lease_owner,
                )

    # ---- 重跑恢复 ----

    async def recover(self, run_id: str) -> list[PublishPlan]:
        """加载可恢复的发布计划（prepared/publishing/partial/cleanup_pending）。

        重跑方继续调用 publish()，DB 映射 + marker 保证幂等零增量。
        """
        return await self.storage.load_recoverable_plans(run_id)


__all__ = ["Publisher"]
