"""ReviewService——三版共用骨架编排（review/service.py，04 §1）。

preflight → fetch(锁定 SHA) → diff 解析与过滤 → 上下文构建（per-file units）
→ ReviewStrategy.execute（版本差异点）→ 统一 FindingPipeline → 落库。
发布（Publishing）属 V1-e，本版本到"产出定稿 Finding + 落库"为止。
"""

from __future__ import annotations

import uuid

from ..config.settings import Settings
from ..domain.diff import FileFilterResult, filter_files, parse_unified_diff
from ..domain.enums import ReviewRunStatus, StageName, StageStatus
from ..domain.finding import Finding
from ..domain.models import ChangedFile, GlobalBudget, ReviewUnit
from ..domain.protocols import GitProvider, LLMProvider, Storage
from ..domain.run import ReviewRun, StageResult
from ..domain.strategy import ReviewStrategy
from .context import ContextAssembler
from .pipeline import FindingPipeline
from .single_pass import SinglePassReviewer


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
    ) -> None:
        self.git = git
        self.llm = llm
        self.storage = storage
        self.settings = settings or Settings()
        self.assembler = ContextAssembler()
        self.strategy = strategy or SinglePassReviewer(
            llm,
            file_tasks=self.settings.concurrency.file_tasks,
            model_requests=self.settings.concurrency.model_requests,
            input_price_per_1k=self.settings.llm.input_price_per_1k,
            output_price_per_1k=self.settings.llm.output_price_per_1k,
        )

    async def review(
        self,
        ref: str,
        *,
        dry_run: bool = True,  # V1-e 使用；本版本先记录
    ) -> tuple[ReviewRun, list[Finding]]:
        run = ReviewRun(run_id=f"run-{uuid.uuid4().hex[:12]}")
        _ = dry_run
        file_map: dict[str, ChangedFile] = {}
        current_stage = StageName.PREFLIGHT
        try:
            # preflight：ChangeRequest 契约校验（head 锁定铁律）
            run.stages.append(StageResult(stage=StageName.PREFLIGHT, status=StageStatus.OK))
            current_stage = StageName.FETCH  # P2-2：进入获取操作前设置，失败归因 FETCH
            req = await self.git.get_changes(ref)
            req.require_head_locked()
            run.external_ref = req.external_id
            run.base_sha = req.base.sha
            run.head_sha = req.head.sha
            run.stages.append(
                StageResult(stage=StageName.FETCH, status=StageStatus.OK, detail=f"head={req.head.sha}")
            )

            # fetch + diff 解析与过滤
            diff_text = await self.git.get_diff(req.base.sha, req.head.sha)
            files = parse_unified_diff(diff_text)
            filtered: FileFilterResult = filter_files(
                files,
                languages=self.settings.review.languages,
                max_files=self.settings.review.max_files,
            )
            file_map = {f.path: f for f in filtered.kept}
            run.warnings.extend(
                f"跳过文件 {f.path}: {reason}" for f, _reason, reason in filtered.skipped
            )
            current_stage = StageName.CONTEXT
            run.stages.append(
                StageResult(
                    stage=StageName.CONTEXT,
                    status=StageStatus.OK,
                    detail=f"kept={len(filtered.kept)} skipped={len(filtered.skipped)}",
                )
            )

            # 上下文构建：per-file units
            units: list[ReviewUnit] = []
            for f in filtered.kept:
                units.extend(
                    self.assembler.build_file_units(run_id=run.run_id, change_request=req, file=f)
                )

            # 全局预算
            budget = GlobalBudget(
                max_total_tokens=self.settings.budget.max_total_tokens,
                max_cost_usd=self.settings.budget.max_cost_usd,
                max_runtime_seconds=self.settings.budget.max_runtime_seconds,
            )

            # strategy.execute（版本差异点）
            current_stage = StageName.REVIEW
            result = await self.strategy.execute(units, run, budget)
            run.warnings.extend(result.source_run.warnings)
            run.stages.append(
                StageResult(
                    stage=StageName.REVIEW,
                    status=StageStatus.OK,
                    detail=f"units={len(units)} candidates={len(result.candidates)}",
                    tokens=budget.tokens_used,
                    cost_usd=budget.cost_used,
                )
            )

            # 统一 FindingPipeline（唯一生命周期所有者）
            current_stage = StageName.PIPELINE
            pipeline = FindingPipeline(
                repo=self.settings.project.name,
                head_sha=req.head.sha,
                min_confidence=self.settings.review.min_confidence,
            )
            findings = pipeline.process(
                run_id=run.run_id,
                candidates=result.candidates,
                file_map=file_map,
            )
            run.stages.append(
                StageResult(
                    stage=StageName.PIPELINE,
                    status=StageStatus.OK,
                    detail=f"findings={len(findings)}",
                )
            )

            # 落库
            await self.storage.record_run(run)
            await self.storage.record_findings(findings)
            for usage in result.source_run.usages:
                await self.storage.record_usage(run.run_id, usage)

            run.finish(
                ReviewRunStatus.COMPLETED if not run.warnings else ReviewRunStatus.PARTIAL
            )
            await self.storage.record_run(run)  # 状态定稿后再写一次（幂等覆盖）
            return run, findings
        except Exception as exc:  # noqa: BLE001
            run.finish(ReviewRunStatus.FAILED)
            run.warnings.append(f"审查中止: {type(exc).__name__}: {exc}")
            run.stages.append(
                StageResult(stage=current_stage, status=StageStatus.FAILED, error=str(exc)[:300])
            )
            await self.storage.record_run(run)
            raise
