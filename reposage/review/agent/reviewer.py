"""AgenticReviewer（24 §8.2）。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

from reposage.config.settings import AgentConfig
from reposage.domain.enums import ReviewStrategyName, ReviewTaskKind, ReviewTaskStatus
from reposage.domain.finding import FindingCandidate
from reposage.domain.models import AgentBudget, GlobalBudget, ModelUsage, ReviewUnit
from reposage.domain.protocols import LLMProvider, Storage
from reposage.domain.run import ReviewRun, ReviewTask, SourceRunResult, StrategyHealth
from reposage.domain.strategy import StrategyResult
from reposage.observability.logging import StructuredLogger
from reposage.review.agent.budget import FinalizeBudgetCoordinator
from reposage.review.agent.loop import run_agent_loop
from reposage.review.agent.session import AgentSession
from reposage.review.agent.workspace import AgentWorkspaceFactory
from reposage.review.context import unit_to_messages
from reposage.tools.registry import ToolRegistry

_PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "tasks" / "agent.md"


def _agent_system(protocol: str) -> str:
    body = _PROMPT.read_text(encoding="utf-8") if _PROMPT.is_file() else "受控 Agent 审查。"
    if protocol == "action_json":
        extra = (
            "本轮使用 Action JSON：只输出一个 JSON 对象，"
            '{"action":"tool_call|submit_finding|finish_review|none","name":str,"args":{}}。'
        )
    else:
        extra = "本轮使用 native tool calling：通过 tools 接口调用，不要假装已经拿到工具结果。"
    return f"{body}\n\n{extra}"


class AgenticReviewer:
    name = ReviewStrategyName.AGENTIC.value

    def __init__(
        self,
        llm: LLMProvider,
        *,
        registry: ToolRegistry,
        workspace_factory: AgentWorkspaceFactory,
        storage: Storage,
        coordinator: FinalizeBudgetCoordinator,
        agent: AgentConfig,
        file_tasks: int = 3,
        model_requests: int = 3,
        max_output_tokens: int = 3000,
        save_tool_trace: bool = True,
        logger: StructuredLogger | None = None,
        input_price_per_1k: float | None = None,
        output_price_per_1k: float | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.workspace_factory = workspace_factory
        self.storage = storage
        self.coordinator = coordinator
        self.agent = agent
        self.file_tasks = file_tasks
        self.model_requests = model_requests
        self.max_output_tokens = max_output_tokens
        self.save_tool_trace = save_tool_trace
        self.logger = logger or StructuredLogger()
        self.input_price_per_1k = input_price_per_1k
        self.output_price_per_1k = output_price_per_1k

    def supports(self, run: ReviewRun) -> bool:
        return run.strategy is ReviewStrategyName.AGENTIC

    async def execute(
        self,
        units: list[ReviewUnit],
        run: ReviewRun,
        budget: GlobalBudget,
    ) -> StrategyResult:
        by_file: dict[str, list[ReviewUnit]] = defaultdict(list)
        for unit in units:
            by_file[unit.file_path].append(unit)

        file_sem = asyncio.Semaphore(self.file_tasks)
        model_sem = asyncio.Semaphore(self.model_requests)
        futs = [
            asyncio.create_task(
                self._review_file(run, path, file_units, file_sem, model_sem, budget)
            )
            for path, file_units in by_file.items()
        ]
        try:
            raw = await asyncio.gather(*futs, return_exceptions=True)
        except asyncio.CancelledError:
            for fut in futs:
                fut.cancel()
            await asyncio.gather(*futs, return_exceptions=True)
            raise

        if any(isinstance(item, asyncio.CancelledError) for item in raw):
            for fut in futs:
                if not fut.done():
                    fut.cancel()
            await asyncio.gather(*futs, return_exceptions=True)
            raise asyncio.CancelledError

        candidates: list[FindingCandidate] = []
        usages: list[ModelUsage] = []
        tasks: list[ReviewTask] = []
        warnings: list[str] = []
        for path, item in zip(by_file.keys(), raw, strict=True):
            if isinstance(item, BaseException):
                warnings.append(f"{path} Agent 失败: {type(item).__name__}: {item}")
                tasks.append(
                    ReviewTask(
                        task_id=f"{run.run_id}:{path}:agent",
                        run_id=run.run_id,
                        kind=ReviewTaskKind.AGENT_TASK,
                        target=path,
                        status=ReviewTaskStatus.FAILED,
                        error=str(item)[:300],
                    )
                )
                continue
            task, file_cands, file_usages = item
            tasks.append(task)
            candidates.extend(file_cands)
            usages.extend(file_usages)
        failed_n = sum(
            1
            for t in tasks
            if t.status in {ReviewTaskStatus.FAILED, ReviewTaskStatus.CANCELLED}
        )
        health = StrategyHealth(
            required_failed=failed_n > 0,
            required_failure_count=failed_n,
            optional_failure_count=0,
            coverage_complete=failed_n == 0,
        )
        return StrategyResult(
            candidates,
            SourceRunResult(
                strategy=ReviewStrategyName.AGENTIC,
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
    ) -> tuple[ReviewTask, list[FindingCandidate], list[ModelUsage]]:
        async with file_sem:
            task = ReviewTask(
                task_id=f"{run.run_id}:{path}:agent",
                run_id=run.run_id,
                kind=ReviewTaskKind.AGENT_TASK,
                target=path,
                status=ReviewTaskStatus.PENDING,
            )
            session_budget = AgentBudget(
                max_rounds=self.agent.max_rounds,
                max_tool_calls=self.agent.max_tool_calls,
                max_tool_attempts=self.agent.effective_max_tool_attempts(),
                max_wallclock_s=self.agent.max_wallclock_s,
                grace_rounds=self.agent.grace_rounds,
                repeat_threshold=self.agent.repeat_threshold,
                compact_threshold_ratio=self.agent.compact_threshold_ratio,
                compact_keep_rounds=self.agent.compact_keep_rounds,
                max_session_chars=self.agent.max_session_chars,
            )
            session = AgentSession(task, file_path=path, budget=session_budget)
            session.add_system(_agent_system(self.agent.tool_protocol))
            user_parts: list[str] = []
            for unit in file_units:
                messages = unit_to_messages(unit)
                for msg in messages:
                    if msg["role"] == "user":
                        user_parts.append(msg["content"])
            session.add_user("\n\n".join(user_parts) if user_parts else f"审查文件 {path}")
            workspace = self.workspace_factory.build(path, task.task_id, run.run_id)
            usages = await run_agent_loop(
                session,
                llm=self.llm,
                registry=self.registry,
                workspace=workspace,
                global_budget=budget,
                coordinator=self.coordinator,
                store=self.storage,
                protocol=self.agent.tool_protocol,
                save_tool_trace=self.save_tool_trace,
                logger=self.logger,
                model_sem=model_sem,
                max_output_tokens=self.max_output_tokens,
                input_price_per_1k=self.input_price_per_1k,
                output_price_per_1k=self.output_price_per_1k,
            )
            task.input_tokens = sum(u.input_tokens for u in usages)
            task.output_tokens = sum(u.output_tokens for u in usages)
            task.cost_usd = sum(u.cost_usd for u in usages)
            await self.storage.record_tasks([task])
            return task, list(session.candidates), usages
