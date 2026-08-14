"""SinglePassReviewer（review/single_pass.py，V1，03 §5 / 04 §1-§2）。

per-file map-reduce（P1-1 返工）：
- 按 file_path 分组 units；
- 文件组之间受 file_tasks Semaphore 限并发；
- 同一文件的多个 unit 串行调用并在文件内 reduce（大文件成本控制）；
- 所有真实模型请求再受共享 model_requests Semaphore 限流（file_tasks 与
  model_requests 是两个独立约束，settings.concurrency）；
- 每个 changed file 生成一个 ReviewTask；块失败按策略标记该文件任务并保留
  已成功块的结果。

硬预算（P0-1 返工）：发送前经 GlobalBudget.reserve() 原子预留（input +
max_output token 与最坏费用，且墙钟未到期）；请求成功/失败后 settle() 按
实际 usage 结算并释放未用预留——检查与占用之间无并发窗口。

铁律：只产出 CandidateFinding + SourceRunResult；不推进 Finding 正式状态。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from ..domain.enums import ReviewStrategyName, ReviewTaskKind, ReviewTaskStatus
from ..domain.finding import FindingCandidate
from ..domain.models import GlobalBudget, ModelUsage, ReviewUnit
from ..domain.protocols import LLMProvider
from ..domain.run import ReviewRun, ReviewTask, SourceRunResult
from ..domain.strategy import StrategyResult
from .context import unit_to_messages


class BudgetExceeded(RuntimeError):
    """全局预算不足/墙钟到期，拒绝发起新模型调用（10 §7 硬预算）。"""


class SinglePassReviewer:
    """V1 单一角色按文件审查（per-file map-reduce）。"""

    name = ReviewStrategyName.SINGLE_PASS.value

    def __init__(
        self,
        llm: LLMProvider,
        *,
        file_tasks: int = 3,
        model_requests: int = 3,
    ) -> None:
        self.llm = llm
        self.file_tasks = file_tasks
        self.model_requests = model_requests

    def supports(self, run: ReviewRun) -> bool:
        return run.strategy is ReviewStrategyName.SINGLE_PASS

    async def execute(
        self,
        units: list[ReviewUnit],
        run: ReviewRun,
        budget: GlobalBudget,
    ) -> StrategyResult:
        """map：文件组并发（file_tasks），组内 unit 串行（model_requests 全局限流）→ reduce 汇总。"""
        by_file: dict[str, list[ReviewUnit]] = defaultdict(list)
        for unit in units:
            by_file[unit.file_path].append(unit)

        file_sem = asyncio.Semaphore(self.file_tasks)
        model_sem = asyncio.Semaphore(self.model_requests)
        results = await asyncio.gather(
            *(
                self._review_file(run, path, file_units, file_sem, model_sem, budget)
                for path, file_units in by_file.items()
            ),
            return_exceptions=True,
        )

        candidates: list[FindingCandidate] = []
        usages: list[ModelUsage] = []
        tasks: list[ReviewTask] = []
        warnings: list[str] = []
        for path, res in zip(by_file.keys(), results, strict=True):
            if isinstance(res, BaseException):
                warnings.append(f"{path} 审查失败: {type(res).__name__}: {res}")
                tasks.append(
                    ReviewTask(
                        task_id=path,
                        run_id=run.run_id,
                        kind=ReviewTaskKind.FILE_REVIEW,
                        target=path,
                        status=ReviewTaskStatus.FAILED,
                        error=str(res)[:300],
                    )
                )
                continue
            task, file_candidates, file_usages, error = res
            candidates.extend(file_candidates)
            usages.extend(file_usages)
            tasks.append(task)
            if error is not None:
                warnings.append(f"{path} 部分块失败: {error}")
        return StrategyResult(
            candidates,
            SourceRunResult(
                strategy=ReviewStrategyName.SINGLE_PASS,
                tasks=tasks,
                usages=usages,
                warnings=warnings,
            ),
        )

    async def _review_file(
        self,
        run: ReviewRun,
        path: str,
        file_units: list[ReviewUnit],
        file_sem: asyncio.Semaphore,
        model_sem: asyncio.Semaphore,
        budget: GlobalBudget,
    ) -> tuple[ReviewTask, list[FindingCandidate], list[ModelUsage], str | None]:
        """单个文件：组内 units 串行（大文件块不重叠执行）；任一块失败中止后续并保留可用结果。"""
        async with file_sem:
            file_candidates: list[FindingCandidate] = []
            file_usages: list[ModelUsage] = []
            first_error: str | None = None
            input_tokens = output_tokens = 0
            cost_usd = 0.0
            for unit in file_units:
                try:
                    cands, usage = await self._review_unit(unit, model_sem, budget)
                except BaseException as exc:  # 块失败：中止该文件剩余块，保留已成功结果
                    first_error = f"{unit.unit_id}: {type(exc).__name__}: {exc}"
                    break
                file_candidates.extend(cands)
                file_usages.append(usage)
                input_tokens += usage.input_tokens
                output_tokens += usage.output_tokens
                cost_usd += usage.cost_usd
            task = ReviewTask(
                task_id=path,
                run_id=run.run_id,
                kind=ReviewTaskKind.FILE_REVIEW,
                target=path,
                status=ReviewTaskStatus.FAILED if first_error else ReviewTaskStatus.COMPLETED,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                error=first_error,
            )
            return task, file_candidates, file_usages, first_error

    async def _review_unit(
        self,
        unit: ReviewUnit,
        model_sem: asyncio.Semaphore,
        budget: GlobalBudget,
    ) -> tuple[list[FindingCandidate], ModelUsage]:
        """单 unit 审查：模型请求限流 + 原子预留 + structured + 结算。"""
        async with model_sem:
            reservation = await budget.reserve(
                input_tokens=unit.context.total_tokens,
                max_output_tokens=unit.output_reserve_tokens,
                est_cost_usd=0.0,  # V1 未定价（V1-f 成本监控补价格）
            )
            if reservation is None:
                raise BudgetExceeded(
                    f"全局预算不足或墙钟到期（tokens_used={budget.tokens_used}），拒绝 {unit.unit_id}"
                )
            try:
                messages = unit_to_messages(unit)
                candidates, usage = await self.llm.structured(messages)
            except BaseException:
                await budget.settle(
                    reservation, actual_input=0, actual_output=0, actual_cost=0.0
                )
                raise
            await budget.settle(
                reservation,
                actual_input=usage.input_tokens,
                actual_output=usage.output_tokens,
                actual_cost=usage.cost_usd,
            )
            return candidates, usage


__all__ = ["BudgetExceeded", "SinglePassReviewer"]
