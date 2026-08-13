"""Finding 模型与生命周期（domain/finding.py）。

契约来源：05 §3（三重身份 / claimed→canonical / 生命周期状态机）。
模型永远只产出 FindingCandidate；正式 Finding 与状态推进仅由程序（统一 FindingPipeline）完成。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .enums import FindingCategory, FindingStatus, Severity
from .models import Evidence, FindingSource, utcnow


class FindingCandidate(BaseModel):
    """模型提交的候选（位置为线索，非事实）。

    claimed_* 由模型提供；canonical_* 由程序重定位并写入正式 Finding。
    """

    title: str
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    category: FindingCategory
    claimed_path: str | None = None
    claimed_start_line: int | None = None
    claimed_end_line: int | None = None
    chunk_id: str | None = None
    evidence_ref: str | None = None
    trigger_condition: str = ""
    impact: str = ""
    explanation: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    suggestion: str = ""
    is_outside_diff: bool = False


class FindingVersion(BaseModel):
    """状态变化审计记录。"""

    from_status: FindingStatus
    to_status: FindingStatus
    actor: str = Field(description="program | judge | user")
    at: datetime = Field(default_factory=utcnow)
    reason: str = ""


class FindingStateError(ValueError):
    """非法 Finding 状态转换（05 §3 状态机断言）。"""


# 合法转换表（05 §3；终态 published/suppressed 不可继续转换，publish_failed→published 例外）
ALLOWED_TRANSITIONS: dict[FindingStatus, set[FindingStatus]] = {
    FindingStatus.CANDIDATE: {FindingStatus.SCHEMA_VALID, FindingStatus.SUPPRESSED},
    FindingStatus.SCHEMA_VALID: {
        FindingStatus.LOCATION_VALID,
        FindingStatus.BODY_ONLY,
        FindingStatus.SUPPRESSED,
    },
    FindingStatus.LOCATION_VALID: {FindingStatus.EVIDENCE_VALID, FindingStatus.SUPPRESSED},
    FindingStatus.EVIDENCE_VALID: {FindingStatus.MERGED, FindingStatus.SUPPRESSED},
    FindingStatus.MERGED: {FindingStatus.ACCEPTED, FindingStatus.SUPPRESSED, FindingStatus.BODY_ONLY},
    FindingStatus.ACCEPTED: {FindingStatus.PUBLISHED, FindingStatus.PUBLISH_FAILED},
    FindingStatus.BODY_ONLY: {FindingStatus.PUBLISHED, FindingStatus.SUPPRESSED},
    FindingStatus.PUBLISH_FAILED: {FindingStatus.PUBLISHED},
    FindingStatus.PUBLISHED: set(),
    FindingStatus.SUPPRESSED: set(),
}


class Finding(BaseModel):
    """正式 Finding（程序驱动生命周期）。"""

    finding_occurrence_id: str = Field(description="UUID，本次 run 记录主键")
    run_id: str
    fingerprint: str = Field(description="run 内去重指纹（聚类结果计算）")
    cross_run_match_key: str = Field(description="跨 run 匹配键（容忍代码移动）")
    cluster_id: str | None = None
    title: str
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    category: FindingCategory
    claimed_path: str | None = None
    claimed_start_line: int | None = None
    claimed_end_line: int | None = None
    canonical_path: str | None = None
    canonical_start_line: int | None = None
    canonical_end_line: int | None = None
    trigger_condition: str = ""
    impact: str = ""
    explanation: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    suggestion: str = ""
    sources: list[FindingSource] = Field(default_factory=list)
    is_outside_diff: bool = False
    status: FindingStatus = FindingStatus.CANDIDATE
    versions: list[FindingVersion] = Field(default_factory=list)

    def record_transition(
        self,
        to: FindingStatus,
        actor: str = "program",
        reason: str = "",
    ) -> Finding:
        """仅程序调用：推进状态并记录审计。

        非法转换（不在 ALLOWED_TRANSITIONS 中，或从终态继续转换）抛 FindingStateError。
        """
        allowed = ALLOWED_TRANSITIONS[self.status]
        if to not in allowed:
            raise FindingStateError(
                f"非法状态转换: {self.status.value} -> {to.value}（允许: {sorted(a.value for a in allowed)}）"
            )
        self.versions.append(
            FindingVersion(from_status=self.status, to_status=to, actor=actor, reason=reason)
        )
        self.status = to
        return self


def _norm(value: Any) -> str:
    """指纹输入归一化。"""
    return str(value or "").strip().lower()


def compute_fingerprint(
    repo: str,
    head_sha: str,
    canonical_path: str | None,
    line_anchor: int | None,
    category: FindingCategory,
    rule_or_issue_key: str | None,
) -> str:
    """run 内去重指纹：含 head_sha 与行锚（05 §3，最终命名）。"""
    payload = "|".join(
        [
            _norm(repo),
            _norm(head_sha),
            _norm(canonical_path),
            str(line_anchor if line_anchor is not None else ""),
            _norm(category.value),
            _norm(rule_or_issue_key),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_cross_run_match_key(
    repo: str,
    canonical_path: str | None,
    symbol_or_anchor: str | None,
    category: FindingCategory,
    rule_or_issue_key: str | None,
) -> str:
    """跨 run 匹配键：不含 head_sha 与行号，容忍代码移动。"""
    payload = "|".join(
        [
            _norm(repo),
            _norm(canonical_path),
            _norm(symbol_or_anchor),
            _norm(category.value),
            _norm(rule_or_issue_key),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
