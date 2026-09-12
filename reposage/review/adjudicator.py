"""LlmAdjudicator：Judge 的 LLM 适配（V2-D DP-9/11）。

走 LLMProvider.complete + JudgeBatchOutput，不走 structured() Finding 信封。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid

from pydantic import ValidationError

from ..config.settings import JudgeConfig
from ..domain.enums import ReviewTaskKind, ReviewTaskStatus
from ..domain.finding import Finding
from ..domain.models import CoverageItem, GlobalBudget, ModelUsage
from ..domain.protocols import LLMProvider
from ..domain.run import ReviewTask
from ..observability.logging import StructuredLogger
from ..prompts.loader import load_task_prompt, task_prompt_hash
from ..review.context import estimate_tokens, wrap_untrusted
from ..review.judge import (
    JudgeAdjudicationResult,
    JudgeBatchOutput,
    JudgeDecision,
    judge_skipped_item,
    parse_judge_decisions,
)
from ..review.single_pass import estimate_cost

_TITLE_MAX = 200
_TEXT_MAX = 400
_EVIDENCE_MAX = 200
_PROVIDER_OVERHEAD = 256


class LlmAdjudicator:
    """按文件分块调用 complete；块失败 keep（fail-open）。"""

    def __init__(
        self,
        llm: LLMProvider,
        config: JudgeConfig,
        *,
        model: str = "DP-V4-PRO",
        input_price_per_1k: float | None = None,
        output_price_per_1k: float | None = None,
        logger: StructuredLogger | None = None,
    ) -> None:
        self.llm = llm
        self.config = config
        self.model = model
        self.input_price_per_1k = input_price_per_1k
        self.output_price_per_1k = output_price_per_1k
        self.logger = logger if logger is not None else StructuredLogger()

    async def adjudicate(
        self,
        *,
        run_id: str,
        findings: list[Finding],
        budget: GlobalBudget,
        model_semaphore: asyncio.Semaphore,
    ) -> JudgeAdjudicationResult:
        tasks: list[ReviewTask] = []
        usages: list[ModelUsage] = []
        coverage_items: list[CoverageItem] = []
        warnings: list[str] = []
        partial = False
        decisions: list[JudgeDecision] = []
        t0 = time.perf_counter()
        chunks = chunk_findings_by_path(findings, self.config.max_findings)
        for chunk in chunks:
            chunk_result, task, usage, warn, cov, chunk_partial = await self._adjudicate_chunk(
                run_id=run_id,
                findings=chunk,
                budget=budget,
                model_semaphore=model_semaphore,
            )
            decisions.extend(chunk_result)
            tasks.append(task)
            if usage is not None:
                usages.append(usage)
            if warn:
                warnings.append(warn)
            if cov is not None:
                coverage_items.append(cov)
            if chunk_partial:
                partial = True
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        self.logger.log(
            level="info",
            run_id=run_id,
            stage="pipeline",
            event="judge_adjudicate",
            detail=(
                f"n_in={len(findings)} n_keep=pending n_downrank=pending "
                f"elapsed_ms={elapsed_ms}"
            ),
        )
        return JudgeAdjudicationResult(
            output=JudgeBatchOutput(decisions=decisions),
            tasks=tasks,
            usages=usages,
            coverage_items=coverage_items,
            warnings=warnings,
            partial=partial,
        )

    async def _adjudicate_chunk(
        self,
        *,
        run_id: str,
        findings: list[Finding],
        budget: GlobalBudget,
        model_semaphore: asyncio.Semaphore,
    ) -> tuple[list[JudgeDecision], ReviewTask, ModelUsage | None, str, CoverageItem | None, bool]:
        target = findings[0].canonical_path or findings[0].claimed_path or "unknown"
        task = ReviewTask(
            task_id=f"judge-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            kind=ReviewTaskKind.JUDGE_ADJUDICATE,
            target=str(target),
            status=ReviewTaskStatus.RUNNING,
        )
        messages = _judge_messages(findings)
        input_estimate = sum(estimate_tokens(str(m.get("content", ""))) for m in messages) + _PROVIDER_OVERHEAD
        est_cost = estimate_cost(
            input_estimate,
            self.config.max_output_tokens,
            self.input_price_per_1k,
            self.output_price_per_1k,
        )
        reservation = await budget.reserve(
            input_tokens=input_estimate,
            max_output_tokens=self.config.max_output_tokens,
            est_cost_usd=est_cost,
        )
        if reservation is None:
            task.status = ReviewTaskStatus.FAILED
            task.error = "budget"
            return (
                [],
                task,
                None,
                "judge_skipped:budget",
                judge_skipped_item("budget"),
                True,
            )
        usage: ModelUsage | None = None
        try:
            timeout_s = min(self.config.timeout_seconds, budget.remaining_runtime_seconds)
            if timeout_s <= 0:
                await budget.settle(reservation, actual_input=0, actual_output=0, actual_cost=0.0)
                task.status = ReviewTaskStatus.FAILED
                task.error = "budget"
                return (
                    [],
                    task,
                    None,
                    "judge_skipped:budget",
                    judge_skipped_item("budget"),
                    True,
                )
            async with model_semaphore:
                async with asyncio.timeout(timeout_s):
                    resp = await self.llm.complete(
                        messages,
                        schema=JudgeBatchOutput,
                        temperature=self.config.temperature,
                        max_tokens=self.config.max_output_tokens,
                    )
            completed = resp.usage
            if completed is None:
                await budget.settle(reservation, actual_input=0, actual_output=0, actual_cost=0.0)
                task.status = ReviewTaskStatus.FAILED
                task.error = "schema"
                return (
                    [],
                    task,
                    None,
                    "judge_skipped:schema",
                    judge_skipped_item("schema"),
                    True,
                )
            usage = completed
            usage.role = "judge"
            usage.prompt_hash = task_prompt_hash("judge")
            usage.schema_hash = _schema_hash()
            actual_cost = estimate_cost(
                usage.input_tokens,
                usage.output_tokens,
                self.input_price_per_1k,
                self.output_price_per_1k,
            )
            if actual_cost is not None:
                usage.cost_usd = actual_cost
            await budget.settle(
                reservation,
                actual_input=usage.input_tokens,
                actual_output=usage.output_tokens,
                actual_cost=usage.cost_usd,
            )
            task.input_tokens = usage.input_tokens
            task.output_tokens = usage.output_tokens
            task.cost_usd = usage.cost_usd
            try:
                raw = resp.data if resp.data is not None else json.loads(resp.text or "{}")
            except json.JSONDecodeError:
                task.status = ReviewTaskStatus.FAILED
                task.error = "schema"
                return (
                    [],
                    task,
                    usage,
                    "judge_skipped:schema",
                    judge_skipped_item("schema"),
                    True,
                )
            decisions = parse_judge_decisions(raw)
            task.status = ReviewTaskStatus.COMPLETED
            return decisions, task, usage, "", None, False
        except asyncio.CancelledError:
            await budget.settle(reservation, actual_input=0, actual_output=0, actual_cost=0.0)
            task.status = ReviewTaskStatus.CANCELLED
            raise
        except TimeoutError:
            await budget.settle(
                reservation,
                actual_input=usage.input_tokens if usage is not None else 0,
                actual_output=usage.output_tokens if usage is not None else 0,
                actual_cost=usage.cost_usd if usage is not None else 0.0,
            )
            task.status = ReviewTaskStatus.FAILED
            task.error = "timeout"
            return (
                [],
                task,
                usage,
                "judge_skipped:timeout",
                judge_skipped_item("timeout"),
                True,
            )
        except (ValidationError, Exception) as exc:
            if usage is not None:
                await budget.settle(
                    reservation,
                    actual_input=usage.input_tokens,
                    actual_output=usage.output_tokens,
                    actual_cost=usage.cost_usd,
                )
            else:
                await budget.settle(reservation, actual_input=0, actual_output=0, actual_cost=0.0)
            task.status = ReviewTaskStatus.FAILED
            task.error = type(exc).__name__
            return (
                [],
                task,
                usage,
                "judge_skipped:schema",
                judge_skipped_item("schema"),
                True,
            )


def chunk_findings_by_path(findings: list[Finding], max_findings: int) -> list[list[Finding]]:
    """按 canonical_path 分块，每块 ≤ max_findings。"""
    groups: dict[str, list[Finding]] = {}
    order: list[str] = []
    for finding in findings:
        key = finding.canonical_path or finding.claimed_path or ""
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(finding)
    chunks: list[list[Finding]] = []
    for key in order:
        group = groups[key]
        for i in range(0, len(group), max_findings):
            chunks.append(group[i : i + max_findings])
    return chunks


def _judge_messages(findings: list[Finding]) -> list[dict[str, str]]:
    system = load_task_prompt("judge")
    payload = [_finding_summary(f) for f in findings]
    user = wrap_untrusted(json.dumps(payload, ensure_ascii=False))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _finding_summary(finding: Finding) -> dict[str, object]:
    return {
        "finding_occurrence_id": finding.finding_occurrence_id,
        "title": _clip(finding.title, _TITLE_MAX),
        "category": finding.category.value,
        "severity": finding.severity.value,
        "confidence": finding.confidence,
        "canonical_path": finding.canonical_path,
        "canonical_start_line": finding.canonical_start_line,
        "canonical_end_line": finding.canonical_end_line,
        "trigger_condition": _clip(finding.trigger_condition, _TEXT_MAX),
        "explanation": _clip(finding.explanation, _TEXT_MAX),
        "suggestion": _clip(finding.suggestion, _TEXT_MAX),
        "evidence": [
            {
                "kind": ev.kind.value,
                "location": ev.location,
                "verified": ev.verified,
                "content": _clip(ev.content, _EVIDENCE_MAX),
            }
            for ev in finding.evidence
        ],
        "sources": [
            {
                "kind": src.kind.value,
                "role_id": src.role_id,
                "analyzer_id": src.analyzer_id,
            }
            for src in finding.sources
        ],
    }


def _clip(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit]


def _schema_hash() -> str:
    blob = json.dumps(JudgeBatchOutput.model_json_schema(), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
