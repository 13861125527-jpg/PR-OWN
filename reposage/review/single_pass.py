"""SinglePassReviewer（review/single_pass.py，V1，03 §5 / 04 §1-§2）。

per-file map-reduce：按 changed file 建任务（unit 粒度），文件任务之间用
model semaphore 限并发；每 unit 一次模型结构化审查（超预算文件由装配器产出
多个 unit）；结果汇总（reduce）为 candidates + SourceRunResult。

铁律：只产出 CandidateFinding + SourceRunResult；不推进 Finding 正式状态
（统一 FindingPipeline 的唯一职责）。
"""

from __future__ import annotations

import asyncio

from ..domain.enums import ReviewStrategyName, ReviewTaskKind, ReviewTaskStatus
from ..domain.finding import FindingCandidate
from ..domain.models import GlobalBudget, ModelUsage, ReviewUnit
from ..domain.protocols import LLMProvider
from ..domain.run import ReviewRun, ReviewTask, SourceRunResult
from ..domain.strategy import StrategyResult
from .context import unit_to_messages


class BudgetExceeded(RuntimeError):
    """全局预算不足，拒绝发起新模型调用（10 §7 硬预算）。"""


class SinglePassReviewer:
    """V1 单一角色按文件审查。"""

    name = ReviewStrategyName.SINGLE_PASS.value

    def __init__(
        self,
        llm: LLMProvider,
        *,
        concurrency: int = 3,
    ) -> None:
        self.llm = llm
        self.concurrency = concurrency

    def supports(self, run: ReviewRun) -> bool:
        return run.strategy is ReviewStrategyName.SINGLE_PASS

    async def execute(
        self,
        units: list[ReviewUnit],
        run: ReviewRun,
        budget: GlobalBudget,
    ) -> StrategyResult:
        """map：每个 unit 一个审查任务（文件任务由 semaphore 限并发）→ reduce 汇总。"""
        sem = asyncio.Semaphore(self.concurrency)
        results = await asyncio.gather(
            *(self._review_unit(run, unit, sem, budget) for unit in units),
            return_exceptions=True,
        )

        candidates: list[FindingCandidate] = []
        usages: list[ModelUsage] = []
        tasks: list[ReviewTask] = []
        warnings: list[str] = []
        for unit, res in zip(units, results, strict=True):
            task_id = f"{unit.file_path}:{unit.unit_id}"
            if isinstance(res, BaseException):
                warnings.append(f"{task_id} 审查失败: {type(res).__name__}: {res}")
                tasks.append(
                    ReviewTask(
                        task_id=task_id,
                        run_id=run.run_id,
                        kind=ReviewTaskKind.FILE_REVIEW,
                        target=unit.file_path,
                        status=ReviewTaskStatus.FAILED,
                        error=str(res)[:300],
                    )
                )
                continue
            unit_candidates, usage = res
            candidates.extend(unit_candidates)
            usages.append(usage)
            tasks.append(
                ReviewTask(
                    task_id=task_id,
                    run_id=run.run_id,
                    kind=ReviewTaskKind.FILE_REVIEW,
                    target=unit.file_path,
                    status=ReviewTaskStatus.COMPLETED,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=usage.cost_usd,
                )
            )
        return StrategyResult(
            candidates,
            SourceRunResult(
                strategy=ReviewStrategyName.SINGLE_PASS,
                tasks=tasks,
                usages=usages,
                warnings=warnings,
            ),
        )

    async def _review_unit(
        self,
        run: ReviewRun,
        unit: ReviewUnit,
        sem: asyncio.Semaphore,
        budget: GlobalBudget,
    ) -> tuple[list[FindingCandidate], ModelUsage]:
        """单 unit 审查：admission control → structured → 记账。"""
        async with sem:
            if not budget.can_admit(unit.context.total_tokens, unit.output_reserve_tokens):
                raise BudgetExceeded(
                    f"全局预算不足（已用 {budget.tokens_used}，需 {unit.context.total_tokens}+"
                    f"{unit.output_reserve_tokens}），拒绝 {unit.unit_id}"
                )
            messages = unit_to_messages(unit)
            candidates, usage = await self.llm.structured(messages)
            budget.consume(usage)
            return candidates, usage


__all__ = ["BudgetExceeded", "SinglePassReviewer"]
