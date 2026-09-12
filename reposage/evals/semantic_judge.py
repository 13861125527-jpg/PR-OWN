"""LLM-as-Judge semantic scoring for eval findings.

Category is diagnostic metadata. A match requires the same code location (within
the configured tolerance) and the same underlying defect semantics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from reposage.domain.finding import Finding
from reposage.domain.protocols import LLMProvider
from reposage.evals.dataset import ExpectedFinding


class SemanticJudgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    api_key: str
    base_url: str
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout_seconds: int = Field(default=90, gt=0)
    max_retries: int = Field(default=2, ge=0)
    max_output_tokens: int = Field(default=3000, gt=0)

    @classmethod
    def load(cls, path: Path) -> SemanticJudgeConfig:
        value = cls.model_validate_json(path.read_text(encoding="utf-8"))
        if not value.api_key.strip() or not value.base_url.strip():
            raise ValueError(f"请先填写语义评测 Judge 配置: {path}")
        return value


class SemanticPairDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pair_id: str
    semantically_equivalent: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""


class SemanticJudgeOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decisions: list[SemanticPairDecision] = Field(default_factory=list)


class SemanticMetrics(BaseModel):
    precision: float
    recall: float
    f1: float
    matched: int
    expected: int
    reported: int
    raw_reported: int
    duplicates_suppressed: int
    category_agreement: float
    category_agreement_count: int
    eligible_pairs: int
    decisions: list[SemanticPairDecision] = Field(default_factory=list)


async def compute_semantic_metrics(
    expected: list[ExpectedFinding],
    findings: list[Finding],
    llm: LLMProvider,
    *,
    line_tolerance: int = 5,
    temperature: float = 0.0,
    max_tokens: int = 3000,
) -> SemanticMetrics:
    pairs: list[tuple[int, int, dict[str, Any]]] = []
    for expected_index, exp in enumerate(expected):
        for finding_index, finding in enumerate(findings):
            if exp.path and finding.canonical_path != exp.path:
                continue
            if exp.line is not None:
                line = finding.canonical_start_line
                if line is None or abs(line - exp.line) > line_tolerance:
                    continue
            pair_id = f"e{expected_index}-f{finding_index}"
            pairs.append((expected_index, finding_index, {
                "pair_id": pair_id,
                "expected": {
                    "category": exp.category,
                    "path": exp.path,
                    "line": exp.line,
                    "note": exp.note,
                },
                "finding": finding_summary(finding),
            }))

    decisions: list[SemanticPairDecision] = []
    if pairs:
        prompt = (
            "You are evaluating code-review findings against reference defects. "
            "For every pair, decide whether both describe the same underlying defect, "
            "trigger, and impact. Category labels are non-binding and must not cause a "
            "mismatch when the semantics agree. The path and line tolerance were already "
            "validated. Do not reward a finding that merely mentions the same code but "
            "claims a different bug. Return one decision for every pair_id as a JSON "
            "object with exactly this shape: "
            "{\"decisions\":[{\"pair_id\":\"e0-f0\","
            "\"semantically_equivalent\":true,\"confidence\":0.95,"
            "\"reason\":\"same root cause\"}]}. Do not return an empty decisions array "
            "when input pairs are present. Preserve every pair_id exactly and return one "
            "decision for every input pair.\n\n"
            + json.dumps([payload for _, _, payload in pairs], ensure_ascii=False)
        )
        response = await llm.complete(
            [{"role": "user", "content": prompt}],
            schema=SemanticJudgeOutput,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        raw = response.data if response.data is not None else json.loads(response.text or "{}")
        decisions = SemanticJudgeOutput.model_validate(raw).decisions

    pair_lookup = {payload["pair_id"]: (ei, fi) for ei, fi, payload in pairs}
    candidates: list[tuple[float, int, int]] = []
    valid_decisions: list[SemanticPairDecision] = []
    seen_pair_ids: set[str] = set()
    for decision in decisions:
        if decision.pair_id in seen_pair_ids or decision.pair_id not in pair_lookup:
            continue
        seen_pair_ids.add(decision.pair_id)
        valid_decisions.append(decision)
        if decision.semantically_equivalent:
            ei, fi = pair_lookup[decision.pair_id]
            candidates.append((decision.confidence, ei, fi))

    used_expected: set[int] = set()
    used_findings: set[int] = set()
    matches: list[tuple[int, int]] = []
    for _, expected_index, finding_index in sorted(candidates, reverse=True):
        if expected_index in used_expected or finding_index in used_findings:
            continue
        used_expected.add(expected_index)
        used_findings.add(finding_index)
        matches.append((expected_index, finding_index))

    hits = len(matches)
    duplicate_findings = {
        finding_index
        for _, expected_index, finding_index in candidates
        if expected_index in used_expected and finding_index not in used_findings
    }
    deduplicated_reported = len(findings) - len(duplicate_findings)
    precision = (
        hits / deduplicated_reported
        if deduplicated_reported
        else (1.0 if not expected else 0.0)
    )
    recall = hits / len(expected) if expected else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    category_matches = sum(
        expected[ei].category == findings[fi].category.value for ei, fi in matches
    )
    return SemanticMetrics(
        precision=precision,
        recall=recall,
        f1=f1,
        matched=hits,
        expected=len(expected),
        reported=deduplicated_reported,
        raw_reported=len(findings),
        duplicates_suppressed=len(duplicate_findings),
        category_agreement=category_matches / hits if hits else (1.0 if not expected else 0.0),
        category_agreement_count=category_matches,
        eligible_pairs=len(pairs),
        decisions=valid_decisions,
    )


def finding_summary(finding: Finding) -> dict[str, Any]:
    return {
        "finding_occurrence_id": finding.finding_occurrence_id,
        "title": finding.title,
        "category": finding.category.value,
        "path": finding.canonical_path,
        "start_line": finding.canonical_start_line,
        "end_line": finding.canonical_end_line,
        "trigger_condition": finding.trigger_condition,
        "impact": finding.impact,
        "explanation": finding.explanation,
        "suggestion": finding.suggestion,
    }
