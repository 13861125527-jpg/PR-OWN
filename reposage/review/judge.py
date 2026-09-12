"""Judge 协议、Fake 与裁决应用（V2-D，07 §6 / 21）。

Pipeline 只依赖本模块协议，不 import OpenAI。Judge 不是审查角色。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..domain.enums import (
    CoverageReason,
    FindingCategory,
    FindingSourceKind,
    FindingStatus,
    JudgeAction,
    StageName,
)
from ..domain.finding import Finding
from ..domain.models import CoverageItem, FindingSource, GlobalBudget, ModelUsage
from ..domain.run import ReviewTask
from ..observability.logging import redact_secrets

_REASON_MAX = 200


class JudgeDecision(BaseModel):
    """单条裁决；多余字段丢掉，禁止写入 Finding 事实。"""

    model_config = ConfigDict(extra="ignore")

    finding_occurrence_id: str
    action: JudgeAction
    reason: str = ""
    # 扁平非 nullable 字段兼容仅支持 JSON Schema 子集的 OpenAI-compatible Provider。
    # 空字符串表示未指定；默认值保持旧 Judge 响应向后兼容。
    canonical_category: str = ""
    duplicate_of: str = ""
    semantic_match: bool = False
    contract_violation_verified: bool = False


class JudgeBatchOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decisions: list[JudgeDecision] = Field(default_factory=list)


class JudgeAdjudicationResult(BaseModel):
    output: JudgeBatchOutput
    tasks: list[ReviewTask] = Field(default_factory=list)
    usages: list[ModelUsage] = Field(default_factory=list)
    coverage_items: list[CoverageItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    partial: bool = False


class FindingAdjudicator(Protocol):
    async def adjudicate(
        self,
        *,
        run_id: str,
        findings: list[Finding],
        budget: GlobalBudget,
        model_semaphore: asyncio.Semaphore,
    ) -> JudgeAdjudicationResult: ...


class FakeAdjudicator:
    """单测用：按预设 decisions 返回；可模拟失败/挂起。"""

    def __init__(
        self,
        decisions: list[JudgeDecision] | None = None,
        *,
        fail: bool = False,
        hang: bool = False,
        extra_payload: dict[str, object] | None = None,
    ) -> None:
        self.decisions = list(decisions or [])
        self.fail = fail
        self.hang = hang
        self.extra_payload = extra_payload
        self.calls: list[list[str]] = []

    async def adjudicate(
        self,
        *,
        run_id: str,
        findings: list[Finding],
        budget: GlobalBudget,
        model_semaphore: asyncio.Semaphore,
    ) -> JudgeAdjudicationResult:
        del run_id, budget, model_semaphore
        self.calls.append([f.finding_occurrence_id for f in findings])
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            return JudgeAdjudicationResult(
                output=JudgeBatchOutput(decisions=[]),
                warnings=["judge schema failed"],
                coverage_items=[_judge_skipped_item("schema")],
                partial=True,
            )
        decisions = list(self.decisions)
        if self.extra_payload:
            raw = {
                "decisions": [
                    d.model_dump() | dict(self.extra_payload) for d in decisions
                ]
            }
            decisions = parse_judge_decisions(raw)
        return JudgeAdjudicationResult(output=JudgeBatchOutput(decisions=decisions))


def parse_judge_decisions(raw: object) -> list[JudgeDecision]:
    """逐条校验；非法 action/条目视为缺席（keep）。整批非对象则空列表。"""
    if not isinstance(raw, dict):
        return []
    items = raw.get("decisions")
    if not isinstance(items, list):
        return []
    out: list[JudgeDecision] = []
    for item in items:
        try:
            out.append(JudgeDecision.model_validate(item))
        except ValidationError:
            continue
    return out


def sanitize_judge_reason(reason: str) -> str:
    cleaned = redact_secrets(str(reason or "").strip().replace("\n", " "))
    return cleaned[:_REASON_MAX]


def apply_judge_decisions(
    findings: list[Finding],
    decisions: list[JudgeDecision],
    *,
    judged_ids: set[str],
) -> tuple[int, int]:
    """应用 keep/downrank、规范类别与显式重复关系。缺席 = keep。

    不在 judged_ids 内的 Finding（溢出未送审）保持 MERGED，不加 JUDGE source。
    """
    by_id = {f.finding_occurrence_id: f for f in findings}
    seen: set[str] = set()
    decided: set[str] = set()
    keep_n = 0
    downrank_n = 0
    for decision in decisions:
        oid = decision.finding_occurrence_id
        if oid in seen:
            continue
        seen.add(oid)
        finding = by_id.get(oid)
        if finding is None or oid not in judged_ids:
            continue
        if finding.status is not FindingStatus.MERGED:
            continue
        decided.add(oid)
        if decision.canonical_category:
            with suppress(ValueError):
                finding.category = FindingCategory(decision.canonical_category)
        if decision.action is JudgeAction.DOWNRANK:
            duplicate = by_id.get(decision.duplicate_of or "")
            if (
                duplicate is not None
                and duplicate.finding_occurrence_id in judged_ids
                and duplicate.status is FindingStatus.MERGED
                and duplicate.finding_occurrence_id != oid
                and _same_location(finding, duplicate)
            ):
                for evidence in finding.evidence:
                    if evidence not in duplicate.evidence:
                        duplicate.evidence.append(evidence)
                for source in finding.sources:
                    if source not in duplicate.sources:
                        duplicate.sources.append(source)
            finding.record_transition(
                FindingStatus.SUPPRESSED,
                actor="judge",
                reason=sanitize_judge_reason(decision.reason) or "downrank",
            )
            downrank_n += 1
        else:
            _append_judge_source(finding)
            keep_n += 1
    for finding in findings:
        if (
            finding.finding_occurrence_id in judged_ids
            and finding.status is FindingStatus.MERGED
            and finding.finding_occurrence_id not in decided
        ):
            _append_judge_source(finding)
            keep_n += 1
    return keep_n, downrank_n


def _append_judge_source(finding: Finding) -> None:
    src = FindingSource(kind=FindingSourceKind.JUDGE, confidence=1.0, verified_by="program")
    if src not in finding.sources:
        finding.sources.append(src)


def _same_location(left: Finding, right: Finding) -> bool:
    left_path = left.canonical_path or left.claimed_path
    right_path = right.canonical_path or right.claimed_path
    if not left_path or left_path != right_path:
        return False
    if left.canonical_start_line is None or right.canonical_start_line is None:
        return False
    left_end = left.canonical_end_line or left.canonical_start_line
    right_end = right.canonical_end_line or right.canonical_start_line
    return not (left_end + 1 < right.canonical_start_line or right_end + 1 < left.canonical_start_line)


def _judge_skipped_item(reason: str) -> CoverageItem:
    return CoverageItem(
        target="judge",
        reason=CoverageReason.TRUNCATED,
        stage=StageName.PIPELINE,
        detail=f"judge_skipped:{reason}",
    )


def judge_skipped_item(reason: str) -> CoverageItem:
    return _judge_skipped_item(reason)
