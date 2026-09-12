"""ReviewService——三版共用骨架编排（review/service.py，04 §1）。

preflight → fetch(锁定 SHA) → diff 解析与过滤 → 上下文构建（per-file units）
→ ReviewStrategy.execute（版本差异点）→ 统一 FindingPipeline → 落库。
发布（Publishing）属 V1-e，本版本到"产出定稿 Finding + 落库"为止。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from ..config.settings import Settings
from ..domain.diff import FileFilterResult, filter_files, parse_unified_diff
from ..domain.enums import (
    ContextLayer,
    CoverageReason,
    PublishPlanStatus,
    ReviewRunStatus,
    ReviewStrategyName,
    StageName,
    StageStatus,
)
from ..domain.finding import Finding
from ..domain.models import (
    ChangedFile,
    ChangeRequest,
    CoverageItem,
    CoverageManifest,
    GlobalBudget,
    ReviewUnit,
)
from ..domain.protocols import (
    GitProvider,
    GitSnapshotProvider,
    LLMProvider,
    Storage,
    as_snapshot_provider,
)
from ..domain.run import PublishPlan, ReviewRun, StageResult
from ..domain.strategy import ReviewStrategy
from ..observability.logging import StructuredLogger
from ..publishing.identity import pr_identity_for
from ..publishing.publisher import Publisher
from .context import ContextAssembler
from .pipeline import FindingPipeline
from .single_pass import SinglePassReviewer
from .static.protocol import StaticAnalyzer


class ReviewService:
    """端到端审查编排（V1：SinglePass + FindingPipeline）。"""

    def __init__(
        self,
        git: GitProvider,
        llm: LLMProvider,
        storage: Storage,
        *,
        settings: Settings | None = None,
        strategy: ReviewStrategy | None = None,
        logger: StructuredLogger | None = None,
        snapshot_provider: GitSnapshotProvider | None = None,
        static_analyzer: StaticAnalyzer | None = None,
    ) -> None:
        self.git = git
        self.llm = llm
        self.storage = storage
        self.settings = settings or Settings()
        self.logger = logger if logger is not None else StructuredLogger()
        self.assembler = ContextAssembler.from_settings(self.settings)
        self.snapshot_provider = snapshot_provider or as_snapshot_provider(git)
        self.static_analyzer = static_analyzer
        if strategy is None and self.settings.review.strategy == "agentic":
            if not self.settings.agent.enabled:
                raise ValueError("review.strategy=agentic 需要 agent.enabled=true")
            self._agentic_requested = True
        else:
            self._agentic_requested = False
        if strategy is not None:
            self.strategy = strategy
        elif self.settings.review.strategy == "multi_role":
            from .reviewers.roles.multi_role import MultiRoleReviewer
            from .reviewers.roles.registry import RoleRegistry

            self.strategy = MultiRoleReviewer(
                llm,
                RoleRegistry(self.settings),
                languages=self.settings.review.languages,
                file_tasks=self.settings.concurrency.file_tasks,
                role_tasks=self.settings.concurrency.role_tasks,
                model_requests=self.settings.concurrency.model_requests,
                input_price_per_1k=self.settings.llm.input_price_per_1k,
                output_price_per_1k=self.settings.llm.output_price_per_1k,
            )
        else:
            self.strategy = SinglePassReviewer(
                llm,
                file_tasks=self.settings.concurrency.file_tasks,
                model_requests=self.settings.concurrency.model_requests,
                input_price_per_1k=self.settings.llm.input_price_per_1k,
                output_price_per_1k=self.settings.llm.output_price_per_1k,
            )
        self.publisher = Publisher(
            git,
            storage,
            repo=self.settings.project.name,
            body_threshold_severity=self.settings.publishing.body_threshold_severity,
        )

    async def review(
        self,
        ref: str,
        *,
        dry_run: bool = True,  # V1-e：dry_run 只生成发布计划不发布
        run_id: str | None = None,  # V1-e：重跑恢复时复用同一 run_id 关联历史 plan
    ) -> tuple[ReviewRun, list[Finding]]:
        run = ReviewRun(run_id=run_id or f"run-{uuid.uuid4().hex[:12]}")
        # V1-f：配置快照哈希（可复现标识，剥离 secret）
        run.config_snapshot_hash = self.settings.snapshot_hash()
        try:
            run.strategy = ReviewStrategyName(getattr(self.strategy, "name", "") or "single_pass")
        except ValueError:
            run.strategy = ReviewStrategyName(self.settings.review.strategy)
        if self._agentic_requested:
            run.strategy = ReviewStrategyName.AGENTIC
        file_map: dict[str, ChangedFile] = {}
        current_stage = StageName.PREFLIGHT
        used_incremental = False
        _stage_t0 = time.perf_counter()  # V1-f：阶段耗时测量
        self.logger.log(level="info", run_id=run.run_id, stage="preflight", event="run_start")
        try:
            # preflight：配置契约 + ChangeRequest 将在 FETCH 校验 head 锁定
            static_cfg = self.settings.review.static
            if static_cfg.enabled:
                unknown = [name for name in static_cfg.analyzers if name != "ruff"]
                if unknown or not static_cfg.analyzers:
                    raise ValueError(f"未知静态分析器: {unknown or ['<empty>']}")
            self._append_stage(run,
                StageResult(
                    stage=StageName.PREFLIGHT,
                    status=StageStatus.OK,
                    duration_ms=int((time.perf_counter() - _stage_t0) * 1000),
                )
            )
            _stage_t0 = time.perf_counter()
            current_stage = StageName.FETCH  # P2-2：进入获取操作前设置，失败归因 FETCH
            req = await self.git.get_changes(ref)
            req.require_head_locked()
            run.external_ref = req.external_id
            run.base_sha = req.base.sha
            run.head_sha = req.head.sha
            diff_text, diff_from, used_incremental = await self._fetch_diff(run, req)
            self._append_stage(run,
                StageResult(
                    stage=StageName.FETCH,
                    status=StageStatus.OK,
                    detail=(
                        f"head={req.head.sha} incremental={1 if used_incremental else 0} "
                        f"from={diff_from} to={req.head.sha}"
                    ),
                    duration_ms=int((time.perf_counter() - _stage_t0) * 1000),
                )
            )
            _stage_t0 = time.perf_counter()

            # diff 解析与过滤（归 CONTEXT）
            # 阶段必须在第一个实际操作之前切换：否则 parse/filter 异常
            # 会被错误归因到已经完成的 FETCH，与计时口径不一致。
            current_stage = StageName.CONTEXT
            files = parse_unified_diff(diff_text)
            filtered: FileFilterResult = filter_files(
                files,
                languages=self.settings.review.languages,
                max_files=self.settings.review.max_files,
            )
            file_map = {f.path: f for f in filtered.kept}
            # V1-f：skipped 文件转 CoverageItem（覆盖清单），同时保留 warning
            skipped_coverage = [
                CoverageItem(target=f.path, reason=reason, stage=StageName.FETCH, detail=detail)
                for f, reason, detail in filtered.skipped
            ]
            run.warnings.extend(
                f"跳过文件 {f.path}: {reason}" for f, _reason, reason in filtered.skipped
            )
            # 上下文构建（build_file_units 归 CONTEXT，V1-f 复验：阶段边界修正）
            snapshot = None
            l3_hits = 0
            memories = []
            if self.settings.review.feedback.enabled:
                memories = await self.storage.load_active_feedback(self.settings.project.name)
            self.assembler.feedback = memories
            self.assembler.feedback_repo = self.settings.project.name
            if self.settings.context.symbol_retrieval:
                snap_git = self.snapshot_provider
                if snap_git is None:
                    run.warnings.append(
                        "L3 capability miss: provider lacks get_blob/list_paths"
                    )
                    skipped_coverage.append(
                        CoverageItem(
                            target="l3",
                            reason=CoverageReason.TRUNCATED,
                            stage=StageName.CONTEXT,
                            detail="capability miss: GitSnapshotProvider",
                        )
                    )
                else:
                    from .symbols.snapshot import prepare_l3_snapshot

                    repo_id = str(getattr(snap_git, "repository_id", "unknown"))
                    prepared = await prepare_l3_snapshot(
                        snap_git,
                        req.head.sha,
                        filtered.kept,
                        repository_id=repo_id,
                    )
                    snapshot = prepared.snapshot
                    diag = prepared.diagnostics
                    for fail in diag.io_failures:
                        run.warnings.append(
                            f"L3 blob failed path={fail.path} reason={fail.reason}"
                        )
                    if diag.truncated_by_cap:
                        run.warnings.append(
                            f"L3 candidate cap skipped_by_cap={diag.skipped_by_cap}"
                        )
            units: list[ReviewUnit] = []
            for f in filtered.kept:
                built = self.assembler.build_file_units(
                    run_id=run.run_id,
                    change_request=req,
                    file=f,
                    snapshot=snapshot,
                )
                for unit in built:
                    for item in unit.coverage.items:
                        detail = item.detail or ""
                        if detail.startswith("L3 extraction failed"):
                            run.warnings.append(f"{detail} path={unit.file_path}")
                l3_hits += sum(
                    1 for u in built for c in u.context.chunks if c.layer is ContextLayer.L3
                )
                units.extend(built)
            if snapshot is not None:
                diag = snapshot.diagnostics
                self.logger.log(
                    level="info",
                    run_id=run.run_id,
                    stage="context",
                    event="l3_retrieve",
                    detail=(
                        f"l3_hits={l3_hits} requests_attempted={diag.requests_attempted} "
                        f"blobs_read={diag.blobs_read} candidates_considered="
                        f"{diag.candidates_considered} blobs_kept={diag.blobs_kept} "
                        f"skipped_by_cap={diag.skipped_by_cap} "
                        f"io_failures={len(diag.io_failures)}"
                    ),
                )

            # V1-f：汇总 run 级覆盖清单（skipped 文件 + 各 unit 覆盖；同一文件多 unit 去重）
            run.coverage = self._merge_coverage(units, skipped_coverage)
            self._append_stage(run,
                StageResult(
                    stage=StageName.CONTEXT,
                    status=StageStatus.OK,
                    detail=(
                        f"kept={len(filtered.kept)} skipped={len(filtered.skipped)}"
                        + (f" l3_hits={l3_hits}" if snapshot is not None else "")
                    ),
                    duration_ms=int((time.perf_counter() - _stage_t0) * 1000),
                )
            )
            _stage_t0 = time.perf_counter()

            # 全局预算
            budget = GlobalBudget(
                max_total_tokens=self.settings.budget.max_total_tokens,
                max_cost_usd=self.settings.budget.max_cost_usd,
                max_runtime_seconds=self.settings.budget.max_runtime_seconds,
                reserved_finalize_ratio=(
                    self.settings.agent.reserved_finalize_ratio if self._agentic_requested else 0.10
                ),
            )

            # strategy.execute（版本差异点）；静态分析与之并行（V2-C DP-1）
            current_stage = StageName.REVIEW
            from .static.protocol import AnalyzerRunResult
            from .static.runner import empty_static_result, run_static_analysis

            active_strategy = self.strategy
            if self._agentic_requested:
                from pathlib import Path

                from ..tools import builtin_registry
                from ..tools.snapshot import GitToolSnapshot
                from .agent.budget import FinalizeBudgetCoordinator
                from .agent.reviewer import AgenticReviewer
                from .agent.workspace import AgentWorkspaceFactory

                await self.storage.record_run(run)
                snap_git = self.snapshot_provider
                if snap_git is None:
                    raise ValueError("agentic 需要 GitSnapshotProvider")
                raw_root = getattr(self.git, "repo_root", None)
                sandbox_root = Path(raw_root) if raw_root is not None else None
                factory = AgentWorkspaceFactory(
                    GitToolSnapshot(
                        snap_git,
                        head_sha=req.head.sha,
                        repository_id=str(getattr(snap_git, "repository_id", "")),
                    ),
                    file_map,
                    sandbox_root=sandbox_root,
                )
                active_strategy = AgenticReviewer(
                    self.llm,
                    registry=builtin_registry(),
                    workspace_factory=factory,
                    storage=self.storage,
                    coordinator=FinalizeBudgetCoordinator(budget),
                    agent=self.settings.agent,
                    file_tasks=self.settings.concurrency.file_tasks,
                    model_requests=self.settings.concurrency.model_requests,
                    max_output_tokens=self.settings.llm.max_output_tokens,
                    save_tool_trace=self.settings.observability.save_tool_trace,
                    logger=self.logger,
                    input_price_per_1k=self.settings.llm.input_price_per_1k,
                    output_price_per_1k=self.settings.llm.output_price_per_1k,
                )

            static_result: AnalyzerRunResult = empty_static_result()
            if self.settings.review.static.enabled:
                result, static_result = await asyncio.gather(
                    active_strategy.execute(units, run, budget),
                    run_static_analysis(
                        run_id=run.run_id,
                        files=list(file_map.values()),
                        file_map=file_map,
                        head_sha=req.head.sha,
                        settings=self.settings.review.static,
                        provider=self.snapshot_provider,
                        snapshot=snapshot,
                        analyzer=self.static_analyzer,
                    ),
                )
            else:
                result = await active_strategy.execute(units, run, budget)
            all_candidates = [*result.candidates, *static_result.candidates]
            if static_result.warnings:
                run.warnings.extend(static_result.warnings)
            if self.settings.review.static.enabled:
                self.logger.log(
                    level="info",
                    run_id=run.run_id,
                    stage="review",
                    event="static_analyze",
                    detail=(
                        f"analyzer_id=ruff n_diag={len(static_result.diagnostics)} "
                        f"n_candidates={len(static_result.candidates)} "
                        f"elapsed_ms={static_result.elapsed_ms} status={static_result.status}"
                    ),
                )
            for decision in result.source_run.gate_decisions:
                self.logger.log(
                    level="info",
                    run_id=run.run_id,
                    stage="review",
                    event="gate_decision",
                    detail=(
                        f"path={decision.file_path} role={decision.role_id} "
                        f"enabled={decision.enabled} reason={decision.reason} "
                        f"features={decision.matched_features}"
                    ),
                )
            run.warnings.extend(result.source_run.warnings)
            health = result.source_run.health
            review_status = StageStatus.PARTIAL if health.required_failed else StageStatus.OK
            self._append_stage(run,
                StageResult(
                    stage=StageName.REVIEW,
                    status=review_status,
                    required=True,
                    detail=json.dumps(
                        {
                            "files": len(file_map),
                            "role_tasks": len(result.source_run.tasks),
                            "candidates": len(all_candidates),
                            "required_failed": health.required_failure_count,
                            "optional_failed": health.optional_failure_count,
                            "static_candidates": len(static_result.candidates),
                            "static_diagnostics": len(static_result.diagnostics),
                            "static_status": static_result.status,
                        },
                        ensure_ascii=False,
                    ),
                    tokens=budget.tokens_used,
                    cost_usd=budget.cost_used,
                    duration_ms=int((time.perf_counter() - _stage_t0) * 1000),
                )
            )
            _stage_t0 = time.perf_counter()

            # 统一 FindingPipeline（唯一生命周期所有者）+ 落库（V1-f 复验：落库归 PIPELINE）
            current_stage = StageName.PIPELINE
            judge_cfg = self.settings.review.judge
            pipeline = FindingPipeline(
                repo=self.settings.project.name,
                head_sha=req.head.sha,
                min_confidence=self.settings.review.min_confidence,
                judge_enabled=judge_cfg.enabled,
                max_findings=judge_cfg.max_findings,
            )
            adjudicator = None
            model_sem: asyncio.Semaphore | None = None
            if judge_cfg.enabled:
                from .adjudicator import LlmAdjudicator

                adjudicator = LlmAdjudicator(
                    self.llm,
                    judge_cfg,
                    model=self.settings.llm.model,
                    input_price_per_1k=self.settings.llm.input_price_per_1k,
                    output_price_per_1k=self.settings.llm.output_price_per_1k,
                    logger=self.logger,
                )
                model_sem = asyncio.Semaphore(self.settings.concurrency.model_requests)
            pipe = await pipeline.process(
                run_id=run.run_id,
                candidates=all_candidates,
                file_map=file_map,
                adjudicator=adjudicator,
                budget=budget if judge_cfg.enabled else None,
                model_semaphore=model_sem,
                feedback=memories if self.settings.review.feedback.enabled else [],
            )
            findings = pipe.findings
            run.warnings.extend(pipe.warnings)
            await self.storage.record_run(run)
            await self.storage.record_findings(findings)
            for usage in [*result.source_run.usages, *pipe.usages]:
                await self.storage.record_usage(run.run_id, usage)
            # V1-f：任务记录（成本/Token）+ 覆盖清单（CoverageItem）落库
            await self.storage.record_tasks(
                [*result.source_run.tasks, *static_result.tasks, *pipe.tasks]
            )
            run.coverage = self._merge_coverage(
                units,
                skipped_coverage,
                [*result.source_run.coverage_items, *static_result.coverage_items, *pipe.coverage_items],
            )
            await self.storage.record_coverage(run.run_id, run.coverage)
            await self.storage.record_gate_decisions(run.run_id, result.source_run.gate_decisions)
            metrics = pipe.metrics
            metrics.feedback_l4_injected = sum(
                1
                for item in run.coverage.items
                if item.target.startswith("feedback:") and item.reason is CoverageReason.COVERED
            )
            metrics.feedback_l4_dropped = sum(
                1
                for item in run.coverage.items
                if item.target.startswith("feedback:") and item.reason is CoverageReason.TRUNCATED
            )
            self._append_stage(run,
                StageResult(
                    stage=StageName.PIPELINE,
                    status=StageStatus.PARTIAL if pipe.partial else StageStatus.OK,
                    detail=json.dumps(
                        {
                            "findings": len(findings),
                            "raw_candidates": metrics.raw_candidates,
                            "merged": metrics.merged,
                            "judge_enabled": metrics.judge_enabled,
                            "judge_keep": metrics.judge_keep,
                            "judge_downrank": metrics.judge_downrank,
                            "needs_evidence": metrics.needs_evidence,
                            "duplicate_survival_rate": metrics.duplicate_survival_rate,
                            "dedup_collapse_rate": metrics.dedup_collapse_rate,
                            "feedback_suppressed": metrics.feedback_suppressed,
                            "feedback_l4_injected": metrics.feedback_l4_injected,
                            "feedback_l4_dropped": metrics.feedback_l4_dropped,
                        },
                        ensure_ascii=False,
                    ),
                    tokens=budget.tokens_used,
                    cost_usd=budget.cost_used,
                    duration_ms=int((time.perf_counter() - _stage_t0) * 1000),
                )
            )
            _stage_t0 = time.perf_counter()

            # V1-e：publish 阶段（dry-run / 正式）
            current_stage = StageName.PUBLISH
            plan = self.publisher.build_plan(
                run,
                findings,
                mode="dry_run" if dry_run else "publish",
                allow_supersede_cleanup=not used_incremental,
            )
            if dry_run:
                # P2（返工）：dry-run plan 落库，调用方可查询计划内容
                await self.storage.record_publish_plan(plan)
                run.publish_status = PublishPlanStatus.PREPARED.value
                self._append_stage(run,
                    StageResult(
                        stage=StageName.PUBLISH,
                        status=StageStatus.OK,
                        detail=f"dry-run plan={plan.plan_id} comments={len(plan.comments)}",
                        duration_ms=int((time.perf_counter() - _stage_t0) * 1000),
                    )
                )
            else:
                # 正式发布：按 PR 身份查历史可恢复的 publish plan，但必须校验 head SHA
                # （V1-e 四轮 P0），避免 head A 的旧计划吞掉 head B 的新结果。
                await self._publish_or_recover(plan, run)
            run.finish(
                ReviewRunStatus.PARTIAL
                if health.required_failed or not health.coverage_complete
                else ReviewRunStatus.COMPLETED
            )
            await self.storage.record_run(run)  # 状态定稿后再写一次（幂等覆盖）
            self.logger.log(
                level="info" if run.status is ReviewRunStatus.COMPLETED else "warning",
                run_id=run.run_id,
                stage="publish",
                event="run_completed",
                detail=f"status={run.status.value} publish_status={run.publish_status}",
            )
            return run, findings
        except asyncio.CancelledError:
            run.finish(ReviewRunStatus.CANCELLED)
            self._append_stage(run,
                StageResult(
                    stage=current_stage,
                    status=StageStatus.FAILED,
                    error="cancelled",
                    detail="cancelled",
                    duration_ms=int((time.perf_counter() - _stage_t0) * 1000),
                )
            )
            await self.storage.record_run(run)
            self.logger.log(
                level="warning",
                run_id=run.run_id,
                stage=current_stage.value,
                event="run_cancelled",
                detail="cancelled",
            )
            raise
        except Exception as exc:  # noqa: BLE001
            run.finish(ReviewRunStatus.FAILED)
            run.warnings.append(f"审查中止: {type(exc).__name__}: {exc}")
            self._append_stage(run,
                StageResult(
                    stage=current_stage,
                    status=StageStatus.FAILED,
                    error=str(exc)[:300],
                    # V1-f 复验：失败阶段用当前阶段起点测量耗时，而非默认 0
                    duration_ms=int((time.perf_counter() - _stage_t0) * 1000),
                )
            )
            await self.storage.record_run(run)
            self.logger.log(
                level="error",
                run_id=run.run_id,
                stage=current_stage.value,
                event="run_failed",
                detail=str(exc)[:300],
            )
            raise

    async def _publish_or_recover(self, plan: PublishPlan, run: ReviewRun) -> None:
        """正式发布编排（V1-e 四轮 P0）：head SHA 校验 + 恢复 + 发布。"""
        latest = await self.storage.load_latest_recoverable_publish_plan(plan.pr_identity)
        if latest is None:
            # 无历史可恢复计划 → 直接发布当前 head 的新 plan
            await self._do_publish(plan, run)
            return

        same_head = latest.target_head_sha == run.head_sha
        # cleanup_pending 或 published+未完成清理 都属清理恢复场景（V1-e 四轮复验 should-fix：
        # 后者若 head 不同也需独立恢复清理，不能 mark_obsolete 后让 pending op 悬空）。
        if self.publisher.needs_cleanup_resume(latest):
            if same_head:
                # 同 head：恢复清理（published 时评论已发布，只补清理）
                await self._do_publish(latest, run, note=f"恢复计划 {latest.plan_id}（清理续发）")
            else:
                # 旧 head 的清理任务：独立恢复清理，然后继续发布当前 head 新 plan
                await self._recover_cleanup_only(latest, run)
                await self._do_publish(plan, run)
            return

        # partial/publishing/prepared
        if same_head:
            await self._do_publish(latest, run, note=f"恢复计划 {latest.plan_id}（幂等续发）")
        else:
            # 旧 head 的 partial/publishing/prepared：标记 OBSOLETE，发布当前 head 新 plan
            await self.storage.mark_plan_obsolete(latest.plan_id)
            await self._do_publish(
                plan, run, note=f"旧计划 {latest.plan_id}（head {latest.target_head_sha}）已标记 obsolete"
            )

    async def _recover_cleanup_only(self, plan: PublishPlan, run: ReviewRun) -> None:
        """旧 head 的 cleanup_pending 独立恢复清理（V1-e 四轮 P0），不吞掉新 head 发布。

        清理恢复结果记入 stage detail（正常控制流，不降级分析状态，V1-e 四轮 P2）。
        """
        _t0 = time.perf_counter()  # V1-f 复验：多 PUBLISH StageResult 各自计时
        recovered = await self.publisher.publish(plan)
        self._append_stage(run,
            StageResult(
                stage=StageName.PUBLISH,
                status=StageStatus.OK if recovered.status is PublishPlanStatus.COMPLETED else StageStatus.PARTIAL,
                detail=f"旧计划 {plan.plan_id}（head {plan.target_head_sha}）清理恢复：{recovered.status.value}",
                duration_ms=int((time.perf_counter() - _t0) * 1000),
            )
        )

    async def _do_publish(self, plan: PublishPlan, run: ReviewRun, *, note: str | None = None) -> None:
        """执行发布并记录 publish 阶段结果（V1-e 四轮 P2：恢复成功不降级分析状态）。"""
        _t0 = time.perf_counter()  # V1-f：阶段耗时测量
        plan = await self.publisher.publish(plan)
        run.publish_status = plan.status.value
        run.warnings.extend(plan.warnings)
        detail = f"plan={plan.plan_id} status={plan.status.value}"
        if note:
            detail = f"{note}; {detail}"
        self._append_stage(run,
            StageResult(
                stage=StageName.PUBLISH,
                status=StageStatus.OK if plan.status in (PublishPlanStatus.PUBLISHED, PublishPlanStatus.COMPLETED) else StageStatus.PARTIAL,
                detail=detail,
                duration_ms=int((time.perf_counter() - _t0) * 1000),
            )
        )

    async def _fetch_diff(
        self, run: ReviewRun, req: ChangeRequest
    ) -> tuple[str, str, bool]:
        """选择 FETCH diff 区间。返回 (diff_text, from_sha, used_incremental)。"""
        base_sha = req.base.sha
        head_sha = req.head.sha
        if not self.settings.review.incremental.enabled:
            return await self.git.get_diff(base_sha, head_sha), base_sha, False
        if not run.external_ref:
            run.warnings.append("incremental_skipped:no_pr_identity")
            return await self.git.get_diff(base_sha, head_sha), base_sha, False
        identity = pr_identity_for(run, self.settings.project.name)
        watermark = await self.storage.load_committed_watermark(identity)
        if not watermark:
            return await self.git.get_diff(base_sha, head_sha), base_sha, False
        if watermark == head_sha:
            run.warnings.append("incremental:no_new_commits")
            return await self.git.get_diff(watermark, head_sha), watermark, True
        try:
            return await self.git.get_diff(watermark, head_sha), watermark, True
        except asyncio.CancelledError:
            raise
        except Exception:
            run.warnings.append("incremental_fallback:diff_failed")
            return await self.git.get_diff(base_sha, head_sha), base_sha, False

    @staticmethod
    def _merge_coverage(
        units: list[ReviewUnit],
        skipped_coverage: list[CoverageItem],
        extra: list[CoverageItem] | None = None,
    ) -> CoverageManifest:
        """汇总 run 级覆盖清单（V1-f / V2-A 角色项）。

        skipped 文件 + 各 unit 覆盖 + Strategy 额外项；按 (target, reason, stage, detail) 去重。
        """
        seen: set[tuple[str, str, str, str | None]] = set()
        items: list[CoverageItem] = []
        extra_items = extra or []
        for item in [*skipped_coverage, *(i for u in units for i in u.coverage.items), *extra_items]:
            key = (item.target, item.reason.value, item.stage.value, item.detail)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
        truncated = (
            any(u.coverage.truncated for u in units)
            or any(i.reason is CoverageReason.TRUNCATED for i in skipped_coverage)
            or any(i.reason is CoverageReason.TRUNCATED for i in extra_items)
        )
        return CoverageManifest(items=items, truncated=truncated)

    def _append_stage(self, run: ReviewRun, sr: StageResult) -> None:
        """追加阶段结果并写结构化日志（V1-f：阶段完成/失败事件）。"""
        run.stages.append(sr)
        if sr.status is StageStatus.FAILED:
            self.logger.log(
                level="error",
                run_id=run.run_id,
                stage=sr.stage.value,
                event="stage_failed",
                detail=sr.error or sr.detail,
            )
        else:
            self.logger.log(
                level="info",
                run_id=run.run_id,
                stage=sr.stage.value,
                event="stage_completed",
                detail=sr.detail,
            )
