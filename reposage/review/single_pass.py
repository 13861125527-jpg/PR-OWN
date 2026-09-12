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
from ..domain.models import GlobalBudget, ModelUsage, ModelUsageOutcome, ReviewUnit
from ..domain.protocols import LLMProvider
from ..domain.run import ReviewRun, ReviewTask, SourceRunResult, StrategyHealth
from ..domain.strategy import StrategyResult
from .candidates import prepare_llm_candidate
from .context import unit_to_messages


class BudgetExceeded(RuntimeError):
    """全局预算不足/墙钟到期，拒绝发起新模型调用（10 §7 硬预算）。"""


class LLMTimeoutError(RuntimeError):
    """在途模型调用超过剩余墙钟被取消（V1-d P0-1）。"""


class SinglePassReviewer:
    """V1 单一角色按文件审查（per-file map-reduce）。"""

    name = ReviewStrategyName.SINGLE_PASS.value

    def __init__(
        self,
        llm: LLMProvider,
        *,
        file_tasks: int = 3,
        model_requests: int = 3,
        input_price_per_1k: float | None = None,
        output_price_per_1k: float | None = None,
    ) -> None:
        self.llm = llm
        self.file_tasks = file_tasks
        self.model_requests = model_requests
        self.input_price_per_1k = input_price_per_1k
        self.output_price_per_1k = output_price_per_1k

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
            if isinstance(res, asyncio.CancelledError):
                raise res  # P2-1：外部取消传播，不转成普通文件失败
            if isinstance(res, BaseException):
                warnings.append(f"{path} 审查失败: {type(res).__name__}: {res}")
                tasks.append(
                    ReviewTask(
                        task_id=f"{run.run_id}:{path}",  # V1-f：全局唯一（跨 run 同 path 不撞键）
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
        failed_n = sum(1 for t in tasks if t.status is ReviewTaskStatus.FAILED)
        health = StrategyHealth(
            required_failed=failed_n > 0,
            required_failure_count=failed_n,
            optional_failure_count=0,
            coverage_complete=failed_n == 0,
        )
        return StrategyResult(
            candidates,
            SourceRunResult(
                strategy=ReviewStrategyName.SINGLE_PASS,
                tasks=tasks,
                usages=usages,
                warnings=warnings,
                health=health,
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
                except Exception as exc:  # 块失败（不含 CancelledError）：中止该文件剩余块，保留已成功结果
                    first_error = f"{unit.unit_id}: {type(exc).__name__}: {exc}"
                    # V1-d 十二轮 P1：失败调用也要计入真实成本——若异常携带 usage
                    # （StructuredOutputError），以 outcome=failed 追加到 usages，
                    # 使调用数/token/schema_repairs 完整，不因失败而丢失。
                    failed_usage = getattr(exc, "usage", None)
                    if failed_usage is not None:
                        failed_usage = failed_usage.model_copy(
                            update={"outcome": ModelUsageOutcome.FAILED}
                        )
                        file_usages.append(failed_usage)
                        input_tokens += failed_usage.input_tokens
                        output_tokens += failed_usage.output_tokens
                        cost_usd += failed_usage.cost_usd
                    break
                file_candidates.extend(cands)
                file_usages.append(usage)
                input_tokens += usage.input_tokens
                output_tokens += usage.output_tokens
                cost_usd += usage.cost_usd
            task = ReviewTask(
                task_id=f"{run.run_id}:{path}",  # V1-f：全局唯一（跨 run 同 path 不撞键）
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
        """单 unit 审查：限流 + 原子预留（含最坏费用）+ 在途墙钟超时 + 结算。

        P0-1（V1-d 二轮返工）：
        - est_cost 由价格配置计算（无定价 → None，费用门控标记 unknown，不伪造零成本）；
        - 模型调用包在剩余墙钟 asyncio.timeout 内，超时取消并释放预留；
        - CancelledError 清理预留后重新抛出（传播取消，不转成普通文件失败）。
        """
        async with model_sem:
            # V1-d 四轮：请求前先构造 messages（无副作用），按完整请求文本 + provider
            # overhead 保守估算 input tokens——context.total_tokens 不含 system 指令、
            # 消息包装与 JSON Schema/结构化指令，直接用它预留会低估真实 prompt token。
            messages = unit_to_messages(unit)
            input_estimate = estimate_request_input_tokens(unit, messages)
            est_cost = estimate_worst_cost(
                input_estimate,
                unit.output_reserve_tokens,
                self.input_price_per_1k,
                self.output_price_per_1k,
            )
            reservation = await budget.reserve(
                input_tokens=input_estimate,
                max_output_tokens=unit.output_reserve_tokens,
                est_cost_usd=est_cost,
            )
            if reservation is None:
                raise BudgetExceeded(
                    f"全局预算不足或墙钟到期（tokens_used={budget.tokens_used}），拒绝 {unit.unit_id}"
                )
            try:
                remaining = budget.remaining_runtime_seconds
                if remaining <= 0:
                    raise BudgetExceeded(f"墙钟已到期，拒绝 {unit.unit_id}")
                async with asyncio.timeout(remaining):
                    candidates, usage = await self.llm.structured(messages)
            except BaseException as exc:
                # 任何失败（含 CancelledError/超时）都释放预留，再按语义抛出。
                # V1-d 十三轮 P1：若异常携带 usage（StructuredOutputError），按实际
                # token 定价结算，失败请求消耗进入预算账（不只进评测报表）。
                # V1-d 十四轮 P1：定价结果回写到异常携带的 usage.cost_usd，使
                # 预算账、usage 明细账、ReviewTask 账三者一致（不回写则 task 成本仍 0）。
                failed_usage = getattr(exc, "usage", None)
                if failed_usage is not None and isinstance(failed_usage, ModelUsage):
                    failed_cost = 0.0
                    if self.input_price_per_1k is not None and self.output_price_per_1k is not None:
                        computed = estimate_cost(
                            failed_usage.input_tokens,
                            failed_usage.output_tokens,
                            self.input_price_per_1k,
                            self.output_price_per_1k,
                        )
                        assert computed is not None  # 价格已确认配置
                        failed_cost = computed
                    # 回写定价到异常携带的 usage（_review_file 经 getattr 取得同一引用）
                    failed_usage.cost_usd = failed_cost
                    await budget.settle(
                        reservation,
                        actual_input=failed_usage.input_tokens,
                        actual_output=failed_usage.output_tokens,
                        actual_cost=failed_cost,
                    )
                else:
                    await budget.settle(
                        reservation, actual_input=0, actual_output=0, actual_cost=0.0
                    )
                if isinstance(exc, asyncio.CancelledError):
                    raise  # 传播外部取消
                if isinstance(exc, TimeoutError):
                    raise LLMTimeoutError(f"在途调用超过剩余墙钟 {remaining:.1f}s: {unit.unit_id}") from exc
                raise
            # P0（V1-d 三轮/四轮）：成功后不能把预留费用全部释放并把账面费用归零。
            # - 价格已配置时按实际 token 在生产层重算真实费用，**不截断**——即使超过
            #   预留也完整入账（四轮：min 截断会低估真实账单）；
            # - 实际费用超过预留 → overrun 熔断：停止后续所有调用（四轮）；
            # - 拿不到可靠 token 时保守按最坏预留入账。
            if self.input_price_per_1k is not None and self.output_price_per_1k is not None:
                if usage.input_tokens > 0 or usage.output_tokens > 0:
                    computed_cost = estimate_cost(
                        usage.input_tokens,
                        usage.output_tokens,
                        self.input_price_per_1k,
                        self.output_price_per_1k,
                    )
                    assert computed_cost is not None  # 价格已确认配置
                    actual_cost = computed_cost
                    budget.pricing_status = "known"
                else:
                    # 无 token 数据：不能证明费用低于预留 → 按最坏预留入账（保守）。
                    # 不用 `est or 0.0`：价格已配置时 est 必然非 None，用 None 回退 0
                    # 会把费用归零、再次绕过硬顶（三轮 should-fix）。
                    assert reservation.est_cost_usd is not None
                    actual_cost = reservation.est_cost_usd
                    budget.pricing_status = "estimated"
                # 完整入账，不截断；超预留 → 熔断后续请求
                assert reservation.est_cost_usd is not None
                if actual_cost > reservation.est_cost_usd:
                    budget.pricing_status = "overrun"
                    budget.overrun = True
                usage.cost_usd = actual_cost  # 观测账与预算账同一结算结果
            else:
                actual_cost = usage.cost_usd  # 未定价：门控 unknown，费用为观测值
            await budget.settle(
                reservation,
                actual_input=usage.input_tokens,
                actual_output=usage.output_tokens,
                actual_cost=actual_cost,
            )
            # P0（V1-d 九轮 / 十轮 P1/P2）：per-file 调用边界确定性补全。
            # - 仅 claimed_path is None 且非 outside-diff 才补成 unit.file_path：
            #   outside-diff 候选的空路径是合理语义，强行补会把"当前 diff 之外"结论
            #   伪装成当前文件内问题（十轮 P1）；
            # - 非空但错误的路径保持原样（由 Pipeline unknown-path 抑制），不覆盖；
            # - 用 model_copy 生成新候选，不原地修改 Provider 可能复用的对象（十轮 P2）。
            filled: list[FindingCandidate] = []
            for cand in candidates:
                filled.append(prepare_llm_candidate(cand, file_path=unit.file_path))
            return filled, usage


def estimate_worst_cost(
    input_tokens: int,
    max_output_tokens: int,
    input_price_per_1k: float | None,
    output_price_per_1k: float | None,
) -> float | None:
    """最坏费用估算（V1-d P0-1）：预留口径，output 取最坏（max_output_tokens）。

    任一价格未配置 → None（未定价，不伪造零成本）。
    """
    return estimate_cost(
        input_tokens,
        max_output_tokens,
        input_price_per_1k,
        output_price_per_1k,
    )


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    input_price_per_1k: float | None,
    output_price_per_1k: float | None,
) -> float | None:
    """费用估算（按给定 input/output token 数）。任一价格未配置 → None。"""
    if input_price_per_1k is None or output_price_per_1k is None:
        return None
    return (
        input_tokens / 1000.0 * input_price_per_1k
        + output_tokens / 1000.0 * output_price_per_1k
    )


# Provider 固定 overhead（token）：system 结构化指令 + JSON Schema + 消息包装 +
# 服务端分词差异的安全余量（V1-d 四轮：预留必须覆盖真实 prompt，不能只算 context token）。
_PROVIDER_OVERHEAD_TOKENS = 512


def estimate_request_input_tokens(unit: ReviewUnit, messages: list[dict[str, str]]) -> int:
    """估算完整请求的 input tokens（V1-d 四轮 P0，启发式，非精确 tokenizer）。

    - context.total_tokens 只是装配层估算，不含 system 指令 / 消息 JSON 包装 /
      JSON Schema / 结构化输出指令，以及服务端分词差异；
    - 因此取 max(装配值, 消息文本字符数/4) 再加固定 provider overhead 余量。
    说明（V1-d 五轮 P2）：字符数/4 是启发式上界，只对 ASCII/英文近似成立；
    **不承诺覆盖中文**（中文通常单字符多 token，可能低估）。真实安全由 overrun
    熔断兜底——即便低估，实际费用超预留时仍会完整入账并停止后续请求。
    """
    text_chars = sum(len(m.get("content", "")) for m in messages)
    from_text = text_chars // 4
    return max(unit.context.total_tokens, from_text) + _PROVIDER_OVERHEAD_TOKENS


__all__ = [
    "BudgetExceeded",
    "LLMTimeoutError",
    "SinglePassReviewer",
    "estimate_cost",
    "estimate_request_input_tokens",
    "estimate_worst_cost",
]
