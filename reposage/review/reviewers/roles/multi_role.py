"""MultiRoleReviewer（V2-A）：门控 + 两阶段准入 + 三层并发 + Barrier。

铁律：只产出 CandidateFinding + SourceRunResult；role_id 由程序盖戳。
execute 签名与 V1 相同。Phase 1 required 全部结束后才创建 Phase 2 optional。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence

from reposage.domain.enums import (
    CoverageReason,
    ReviewStrategyName,
    ReviewTaskKind,
    ReviewTaskStatus,
    StageName,
)
from reposage.domain.finding import FindingCandidate
from reposage.domain.models import (
    CoverageItem,
    GlobalBudget,
    ModelUsage,
    ModelUsageOutcome,
    ReviewUnit,
)
from reposage.domain.protocols import LLMProvider
from reposage.domain.run import (
    GateDecision,
    ReviewRun,
    ReviewTask,
    RoleSpec,
    SourceRunResult,
    StrategyHealth,
)
from reposage.domain.strategy import StrategyResult
from reposage.prompts.loader import load_role_prompt, role_prompt_hash
from reposage.review.candidates import prepare_llm_candidate
from reposage.review.context import unit_to_messages
from reposage.review.reviewers.roles.gates import evaluate_gate
from reposage.review.reviewers.roles.registry import RoleRegistry
from reposage.review.single_pass import (
    BudgetExceeded,
    LLMTimeoutError,
    estimate_cost,
    estimate_request_input_tokens,
    estimate_worst_cost,
)


class _RoleOutcome:
    def __init__(
        self,
        *,
        required: bool,
        task: ReviewTask,
        candidates: list[FindingCandidate],
        usages: list[ModelUsage],
        coverage: CoverageItem,
    ) -> None:
        self.required = required
        self.task = task
        self.candidates = candidates
        self.usages = usages
        self.coverage = coverage


class MultiRoleReviewer:
    """条件多角色审查（18 §4/§6）。"""

    name = ReviewStrategyName.MULTI_ROLE.value

    def __init__(
        self,
        llm: LLMProvider,
        registry: RoleRegistry,
        *,
        languages: list[str] | None = None,
        file_tasks: int = 3,
        role_tasks: int = 3,
        model_requests: int = 3,
        input_price_per_1k: float | None = None,
        output_price_per_1k: float | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.languages = languages or ["python"]
        self.file_tasks = file_tasks
        self.role_tasks = role_tasks
        self.model_requests = model_requests
        self.input_price_per_1k = input_price_per_1k
        self.output_price_per_1k = output_price_per_1k
        self._prompts = {spec.id: load_role_prompt(spec.prompt_id) for spec in registry.effective_enabled()}
        self._prompt_hashes = {
            spec.id: role_prompt_hash(spec.prompt_id) for spec in registry.effective_enabled()
        }

    def supports(self, run: ReviewRun) -> bool:
        return run.strategy is ReviewStrategyName.MULTI_ROLE

    async def execute(
        self,
        units: list[ReviewUnit],
        run: ReviewRun,
        budget: GlobalBudget,
    ) -> StrategyResult:
        if not units:
            return StrategyResult(
                [],
                SourceRunResult(strategy=ReviewStrategyName.MULTI_ROLE, health=StrategyHealth()),
            )

        by_file: dict[str, list[ReviewUnit]] = defaultdict(list)
        for unit in units:
            by_file[unit.file_path].append(unit)

        enabled = self.registry.effective_enabled()
        decisions: list[GateDecision] = []
        required_by_file: dict[str, list[RoleSpec]] = {}
        optional_by_file: dict[str, list[RoleSpec]] = {}
        missing_features: set[str] = set()

        for path, file_units in by_file.items():
            features = file_units[0].gate_features
            if features is None or any(u.gate_features is None for u in file_units):
                missing_features.add(path)
                required_by_file[path] = []
                optional_by_file[path] = []
                continue
            req: list[RoleSpec] = []
            opt: list[RoleSpec] = []
            for spec in enabled:
                decision = evaluate_gate(features, spec, self.languages)
                decisions.append(decision)
                if not decision.enabled:
                    continue
                if spec.required:
                    req.append(spec)
                else:
                    opt.append(spec)
            required_by_file[path] = req
            optional_by_file[path] = opt

        file_sem = asyncio.Semaphore(self.file_tasks)
        role_sem = asyncio.Semaphore(self.role_tasks)
        model_sem = asyncio.Semaphore(self.model_requests)

        paths = list(by_file)
        # Phase 1: required only — 禁止在此之前创建 optional coroutine
        phase1 = await asyncio.gather(
            *(
                self._run_file_roles(
                    run,
                    path,
                    by_file[path],
                    required_by_file[path],
                    file_sem,
                    role_sem,
                    model_sem,
                    budget,
                )
                for path in paths
            ),
            return_exceptions=True,
        )
        self._reraise_cancelled(phase1)
        phase1_broken = any(isinstance(res, BaseException) for res in phase1)

        # Phase 2: 仅在 Phase 1 批次全部正常结束（含内部 fail-soft）后创建
        phase2_batches: list[list[_RoleOutcome]] = []
        if not phase1_broken:
            phase2_raw = await asyncio.gather(
                *(
                    self._run_file_roles(
                        run,
                        path,
                        by_file[path],
                        optional_by_file[path],
                        file_sem,
                        role_sem,
                        model_sem,
                        budget,
                    )
                    for path in paths
                ),
                return_exceptions=True,
            )
            self._reraise_cancelled(phase2_raw)
            for res in phase2_raw:
                if isinstance(res, BaseException):
                    raise res
                phase2_batches.append(res)

        phase1_batches: list[list[_RoleOutcome]] = []
        for path, batch in zip(paths, phase1, strict=True):
            if isinstance(batch, BaseException):
                phase1_batches.append(
                    self._structural_fail_outcomes(run, path, required_by_file[path], batch)
                )
            else:
                phase1_batches.append(batch)

        candidates: list[FindingCandidate] = []
        usages: list[ModelUsage] = []
        tasks: list[ReviewTask] = []
        warnings: list[str] = []
        coverage_items: list[CoverageItem] = []
        required_failed = 0
        optional_failed = 0

        for path in missing_features:
            warnings.append(f"{path} 缺少 gate_features，required 未覆盖")
            required_failed += 1
            tasks.append(
                ReviewTask(
                    task_id=f"{run.run_id}:{path}::general",
                    run_id=run.run_id,
                    kind=ReviewTaskKind.ROLE_REVIEW,
                    target=f"{path}::general",
                    status=ReviewTaskStatus.FAILED,
                    error="missing gate_features",
                )
            )
            coverage_items.append(
                CoverageItem(
                    target=f"{path}::general",
                    reason=CoverageReason.TASK_FAILED,
                    stage=StageName.REVIEW,
                    detail="missing gate_features",
                )
            )

        for batch in (*phase1_batches, *phase2_batches):
            for item in batch:
                candidates.extend(item.candidates)
                usages.extend(item.usages)
                tasks.append(item.task)
                coverage_items.append(item.coverage)
                if item.task.status is ReviewTaskStatus.FAILED:
                    if item.required:
                        required_failed += 1
                        warnings.append(f"{item.task.target} required 失败: {item.task.error}")
                    else:
                        optional_failed += 1
                        warnings.append(f"{item.task.target} optional 失败: {item.task.error}")

        kept_with_required_ok = True
        for path in by_file:
            if path in missing_features:
                kept_with_required_ok = False
                continue
            req_specs = required_by_file[path]
            req_tasks = [
                t
                for t in tasks
                if t.target.startswith(f"{path}::")
                and any(t.target.endswith(f"::{s.id}") for s in req_specs)
            ]
            if not req_specs:
                kept_with_required_ok = False
                continue
            if any(t.status is not ReviewTaskStatus.COMPLETED for t in req_tasks) or len(req_tasks) < len(
                req_specs
            ):
                kept_with_required_ok = False

        health = StrategyHealth(
            required_failed=required_failed > 0 or not kept_with_required_ok,
            required_failure_count=required_failed,
            optional_failure_count=optional_failed,
            coverage_complete=kept_with_required_ok and required_failed == 0,
        )

        return StrategyResult(
            candidates,
            SourceRunResult(
                strategy=ReviewStrategyName.MULTI_ROLE,
                tasks=tasks,
                usages=usages,
                warnings=warnings,
                health=health,
                gate_decisions=decisions,
                coverage_items=coverage_items,
            ),
        )

    @staticmethod
    def _reraise_cancelled(results: Sequence[object]) -> None:
        for res in results:
            if isinstance(res, asyncio.CancelledError):
                raise res

    @staticmethod
    def _structural_fail_outcomes(
        run: ReviewRun,
        path: str,
        specs: list[RoleSpec],
        exc: BaseException,
    ) -> list[_RoleOutcome]:
        """Phase 1 批次级异常：记 required 失败并 fail-closed，不进入 Phase 2。"""
        targets = specs or [
            RoleSpec(id="general", prompt_id="general", gate_id="always", required=True)
        ]
        error = f"{type(exc).__name__}: {exc}"[:300]
        return [
            _RoleOutcome(
                required=True,
                task=ReviewTask(
                    task_id=f"{run.run_id}:{path}::{spec.id}",
                    run_id=run.run_id,
                    kind=ReviewTaskKind.ROLE_REVIEW,
                    target=f"{path}::{spec.id}",
                    status=ReviewTaskStatus.FAILED,
                    error=error,
                ),
                candidates=[],
                usages=[],
                coverage=CoverageItem(
                    target=f"{path}::{spec.id}",
                    reason=CoverageReason.TASK_FAILED,
                    stage=StageName.REVIEW,
                    detail=error[:200],
                ),
            )
            for spec in targets
        ]

    async def _run_file_roles(
        self,
        run: ReviewRun,
        path: str,
        file_units: list[ReviewUnit],
        specs: list[RoleSpec],
        file_sem: asyncio.Semaphore,
        role_sem: asyncio.Semaphore,
        model_sem: asyncio.Semaphore,
        budget: GlobalBudget,
    ) -> list[_RoleOutcome]:
        if not specs:
            return []
        async with file_sem:
            gathered = await asyncio.gather(
                *(
                    self._review_role(run, path, file_units, spec, role_sem, model_sem, budget)
                    for spec in specs
                ),
                return_exceptions=True,
            )
            outcomes: list[_RoleOutcome] = []
            for spec, res in zip(specs, gathered, strict=True):
                if isinstance(res, asyncio.CancelledError):
                    raise res
                if isinstance(res, BaseException):
                    outcomes.append(
                        _RoleOutcome(
                            required=spec.required,
                            task=ReviewTask(
                                task_id=f"{run.run_id}:{path}::{spec.id}",
                                run_id=run.run_id,
                                kind=ReviewTaskKind.ROLE_REVIEW,
                                target=f"{path}::{spec.id}",
                                status=ReviewTaskStatus.FAILED,
                                error=f"{type(res).__name__}: {res}"[:300],
                            ),
                            candidates=[],
                            usages=[],
                            coverage=CoverageItem(
                                target=f"{path}::{spec.id}",
                                reason=CoverageReason.ROLE_FAILED,
                                stage=StageName.REVIEW,
                                detail=str(res)[:200],
                            ),
                        )
                    )
                    continue
                outcomes.append(res)
            return outcomes

    async def _review_role(
        self,
        run: ReviewRun,
        path: str,
        file_units: list[ReviewUnit],
        spec: RoleSpec,
        role_sem: asyncio.Semaphore,
        model_sem: asyncio.Semaphore,
        budget: GlobalBudget,
    ) -> _RoleOutcome:
        async with role_sem:
            candidates: list[FindingCandidate] = []
            usages: list[ModelUsage] = []
            first_error: str | None = None
            truncated = False
            input_tokens = output_tokens = 0
            cost_usd = 0.0
            for unit in file_units:
                try:
                    cands, usage = await self._review_unit(unit, spec, model_sem, budget)
                except BudgetExceeded as exc:
                    first_error = f"{unit.unit_id}: {exc}"
                    truncated = True
                    break
                except Exception as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    first_error = f"{unit.unit_id}: {type(exc).__name__}: {exc}"
                    failed_usage = getattr(exc, "usage", None)
                    if failed_usage is not None:
                        failed_usage = failed_usage.model_copy(
                            update={"outcome": ModelUsageOutcome.FAILED, "role": spec.id}
                        )
                        usages.append(failed_usage)
                        input_tokens += failed_usage.input_tokens
                        output_tokens += failed_usage.output_tokens
                        cost_usd += failed_usage.cost_usd
                    break
                candidates.extend(cands)
                usages.append(usage)
                input_tokens += usage.input_tokens
                output_tokens += usage.output_tokens
                cost_usd += usage.cost_usd
            status = ReviewTaskStatus.FAILED if first_error else ReviewTaskStatus.COMPLETED
            reason = (
                CoverageReason.TRUNCATED
                if truncated
                else CoverageReason.ROLE_FAILED
                if first_error
                else CoverageReason.COVERED
            )
            return _RoleOutcome(
                required=spec.required,
                task=ReviewTask(
                    task_id=f"{run.run_id}:{path}::{spec.id}",
                    run_id=run.run_id,
                    kind=ReviewTaskKind.ROLE_REVIEW,
                    target=f"{path}::{spec.id}",
                    status=status,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    error=first_error,
                ),
                candidates=candidates,
                usages=usages,
                coverage=CoverageItem(
                    target=f"{path}::{spec.id}",
                    reason=reason,
                    stage=StageName.REVIEW,
                    detail=first_error,
                ),
            )

    async def _review_unit(
        self,
        unit: ReviewUnit,
        spec: RoleSpec,
        model_sem: asyncio.Semaphore,
        budget: GlobalBudget,
    ) -> tuple[list[FindingCandidate], ModelUsage]:
        async with model_sem:
            messages = unit_to_messages(unit, role_prompt=self._prompts[spec.id])
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
                    f"全局预算不足或墙钟到期（tokens_used={budget.tokens_used}），拒绝 {unit.unit_id}/{spec.id}"
                )
            try:
                remaining = budget.remaining_runtime_seconds
                if remaining <= 0:
                    raise BudgetExceeded(f"墙钟已到期，拒绝 {unit.unit_id}/{spec.id}")
                async with asyncio.timeout(remaining):
                    candidates, usage = await self.llm.structured(
                        messages, prompt_hash=self._prompt_hashes.get(spec.id)
                    )
            except BaseException as exc:
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
                        assert computed is not None
                        failed_cost = computed
                    failed_usage.cost_usd = failed_cost
                    failed_usage.role = spec.id
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
                    raise
                if isinstance(exc, TimeoutError):
                    raise LLMTimeoutError(
                        f"在途调用超过剩余墙钟 {remaining:.1f}s: {unit.unit_id}/{spec.id}"
                    ) from exc
                raise
            if self.input_price_per_1k is not None and self.output_price_per_1k is not None:
                if usage.input_tokens > 0 or usage.output_tokens > 0:
                    computed_cost = estimate_cost(
                        usage.input_tokens,
                        usage.output_tokens,
                        self.input_price_per_1k,
                        self.output_price_per_1k,
                    )
                    assert computed_cost is not None
                    actual_cost = computed_cost
                    budget.pricing_status = "known"
                else:
                    assert reservation.est_cost_usd is not None
                    actual_cost = reservation.est_cost_usd
                    budget.pricing_status = "estimated"
                assert reservation.est_cost_usd is not None
                if actual_cost > reservation.est_cost_usd:
                    budget.pricing_status = "overrun"
                    budget.overrun = True
                usage.cost_usd = actual_cost
            else:
                actual_cost = usage.cost_usd
            await budget.settle(
                reservation,
                actual_input=usage.input_tokens,
                actual_output=usage.output_tokens,
                actual_cost=actual_cost,
            )
            stamped: list[FindingCandidate] = []
            for cand in candidates:
                stamped.append(
                    prepare_llm_candidate(cand, file_path=unit.file_path, role_id=spec.id)
                )
            usage = usage.model_copy(update={"role": spec.id})
            return stamped, usage


__all__ = ["MultiRoleReviewer"]
