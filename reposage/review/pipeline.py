"""统一 FindingPipeline——Finding 正式生命周期唯一所有者（review/pipeline.py，07 §5 / 05 §3）。

ReviewService 编排；任何 Strategy 只返回 FindingCandidate + SourceRunResult，
本流水线执行：schema 校验 → location 校验（claimed→canonical 重定位）→ evidence
校验 → 聚类 → 去重（fingerprint）→ 来源合并 → 门槛（accepted/suppressed）。

铁律（P0-1 / 05 §4）：canonical 事实字段、fingerprint、cluster_id、status、sources
仅程序写入；Pipeline 不修改 canonical 事实，只验证与取舍。
"""

from __future__ import annotations

import uuid

from ..domain.enums import EvidenceKind, FindingSourceKind, FindingStatus, Severity
from ..domain.finding import (
    Finding,
    FindingCandidate,
    compute_cross_run_match_key,
    compute_fingerprint,
)
from ..domain.models import ChangedFile, Evidence, FindingSource
from .location import LOCATION_VALID, LocationResolution, resolve_location

# 聚类行区间重叠容差（同 file + 同 category + 行区间邻近 → 同一问题）
_CLUSTER_LINE_TOLERANCE = 8

# 模型候选无规则键时的规则键归一（参与 fingerprint / cross_run_match_key）
_MODEL_RULE_KEY = "model"


class FindingPipeline:
    """候选 → 正式 Finding（唯一生命周期推进者）。"""

    def __init__(
        self,
        *,
        repo: str,
        head_sha: str,
        min_confidence: float = 0.75,
    ) -> None:
        self.repo = repo
        self.head_sha = head_sha
        self.min_confidence = min_confidence

    def process(
        self,
        *,
        run_id: str,
        candidates: list[FindingCandidate],
        file_map: dict[str, ChangedFile],
    ) -> list[Finding]:
        """完整流水线；返回已定稿（accepted/suppressed/body_only）的 Finding 列表。"""
        validated: list[Finding] = []
        body_only: list[Finding] = []
        for cand in candidates:
            f = self._validate_and_locate(run_id, cand, file_map)
            if f.status is FindingStatus.BODY_ONLY:
                body_only.append(f)  # 终态：不进行内、不聚类（05 §3 状态机）
            else:
                validated.append(f)

        clustered = self._cluster(validated)
        finalized: list[Finding] = list(body_only)
        for cluster in clustered:
            merged = self._merge_cluster(run_id, cluster)
            if merged is None:
                continue
            merged.record_transition(FindingStatus.MERGED, actor="program", reason="聚类去重保留")
            if merged.confidence >= self.min_confidence:
                merged.record_transition(FindingStatus.ACCEPTED, actor="program", reason="置信度门槛")
            else:
                merged.record_transition(
                    FindingStatus.SUPPRESSED, actor="program",
                    reason=f"confidence {merged.confidence:.2f} < {self.min_confidence}",
                )
            finalized.append(merged)

        finalized.sort(key=_sort_key, reverse=True)  # 07 §5 排序器（severity×confidence）
        return finalized

    # ---- 阶段 1：schema + location + evidence 校验 ----

    def _validate_and_locate(
        self,
        run_id: str,
        cand: FindingCandidate,
        file_map: dict[str, ChangedFile],
    ) -> Finding:
        f = Finding(
            finding_occurrence_id=uuid.uuid4().hex,
            run_id=run_id,
            fingerprint="",  # 聚类后计算
            cross_run_match_key="",
            title=cand.title,
            severity=cand.severity,
            confidence=cand.confidence,
            category=cand.category,
            claimed_path=cand.claimed_path,
            claimed_start_line=cand.claimed_start_line,
            claimed_end_line=cand.claimed_end_line,
            trigger_condition=cand.trigger_condition,
            impact=cand.impact,
            explanation=cand.explanation,
            suggestion=cand.suggestion,
            evidence=list(cand.evidence),
            sources=[FindingSource(kind=FindingSourceKind.LLM_GENERAL, confidence=cand.confidence)],
            is_outside_diff=cand.is_outside_diff,
        )

        # schema 校验（防御性：provider 已严格校验，这里再验白名单/范围）
        if not self._schema_valid(cand):
            f.record_transition(FindingStatus.SUPPRESSED, actor="program", reason="schema 校验失败")
            return f
        f.record_transition(FindingStatus.SCHEMA_VALID, actor="program", reason="schema 校验通过")

        # location 校验：claimed → canonical
        res: LocationResolution = resolve_location(cand, file_map)
        f.canonical_path = res.path
        f.canonical_start_line = res.start_line
        f.canonical_end_line = res.end_line
        if res.status == LOCATION_VALID:
            f.record_transition(FindingStatus.LOCATION_VALID, actor="program", reason=res.reason)
        else:
            f.record_transition(FindingStatus.BODY_ONLY, actor="program", reason=res.reason)
            return f  # body_only：不进 evidence/聚类（正文结论，V1-e 归入正文评论）

        # evidence 校验（V1 简化：模型提供触发条件即视为候选证据；V3 严格可追溯）
        if self._evidence_valid(cand):
            f.record_transition(FindingStatus.EVIDENCE_VALID, actor="program", reason="证据线索存在")
        else:
            # 无法验证 → 置信度下调（程序只可下调不可上调，05 §3）
            f.confidence = round(f.confidence * 0.8, 4)
            f.record_transition(
                FindingStatus.EVIDENCE_VALID, actor="program", reason="证据不足，置信度下调"
            )
        return f

    @staticmethod
    def _schema_valid(cand: FindingCandidate) -> bool:
        """防御性 schema 校验（provider 已严格校验；此处仅再验数值范围）。"""
        return 0.0 <= cand.confidence <= 1.0

    @staticmethod
    def _evidence_valid(cand: FindingCandidate) -> bool:
        """证据线索：触发条件/解释/显式证据；位置线索（claimed_path）不算证据。"""
        return bool(cand.trigger_condition or cand.explanation or cand.evidence)

    # ---- 阶段 2：聚类（键 = file + 重叠行区间 + category） ----

    @classmethod
    def _cluster(cls, findings: list[Finding]) -> list[list[Finding]]:
        clusters: list[list[Finding]] = []
        for f in findings:
            placed = False
            for cluster in clusters:
                if cls._same_cluster(cluster[0], f):
                    cluster.append(f)
                    placed = True
                    break
            if not placed:
                clusters.append([f])
        return clusters

    @staticmethod
    def _same_cluster(a: Finding, b: Finding) -> bool:
        if a.category is not b.category:
            return False
        path_a = a.canonical_path or a.claimed_path
        path_b = b.canonical_path or b.claimed_path
        if not path_a or path_a != path_b:
            return False
        if a.canonical_start_line is None or b.canonical_start_line is None:
            return False
        # 行区间重叠/邻近
        a_lo, a_hi = a.canonical_start_line, a.canonical_end_line or a.canonical_start_line
        b_lo, b_hi = b.canonical_start_line, b.canonical_end_line or b.canonical_start_line
        return not (a_hi + _CLUSTER_LINE_TOLERANCE < b_lo or b_hi + _CLUSTER_LINE_TOLERANCE < a_lo)

    # ---- 阶段 3：合并 + fingerprint 去重 + 门槛 ----

    def _merge_cluster(self, run_id: str, cluster: list[Finding]) -> Finding | None:
        """保留最高置信度，合并证据/来源；聚类后计算 fingerprint 去重。"""
        merged = max(cluster, key=lambda f: f.confidence)
        merged.evidence = _merge_evidence(cluster)
        merged.sources = _merge_sources(cluster)
        merged.confidence = max(f.confidence for f in cluster)

        line_anchor = merged.canonical_start_line
        fingerprint = compute_fingerprint(
            repo=self.repo,
            head_sha=self.head_sha,
            canonical_path=merged.canonical_path,
            line_anchor=line_anchor,
            category=merged.category,
            rule_or_issue_key=_MODEL_RULE_KEY,
        )
        cross_key = compute_cross_run_match_key(
            repo=self.repo,
            canonical_path=merged.canonical_path,
            symbol_or_anchor=str(line_anchor) if line_anchor is not None else None,
            category=merged.category,
            rule_or_issue_key=_MODEL_RULE_KEY,
        )
        merged.fingerprint = fingerprint
        merged.cross_run_match_key = cross_key
        merged.cluster_id = uuid.uuid4().hex
        return merged


def _merge_evidence(cluster: list[Finding]) -> list[Evidence]:
    merged: list[Evidence] = []
    for f in cluster:
        for ev in f.evidence:
            if ev not in merged:
                merged.append(ev)
    # 无证据时补一条 diff 锚点证据（程序生成，verified=True）
    anchor = cluster[0]
    if not merged and anchor.canonical_path and anchor.canonical_start_line is not None:
        merged.append(
            Evidence(
                kind=EvidenceKind.DIFF_LINE,
                location=f"{anchor.canonical_path}:{anchor.canonical_start_line}",
                content=anchor.trigger_condition[:200],
                verified=True,
            )
        )
    return merged


def _merge_sources(cluster: list[Finding]) -> list[FindingSource]:
    merged: list[FindingSource] = []
    for f in cluster:
        for src in f.sources:
            if src not in merged:
                merged.append(src)
    return merged


def _severity_rank(sev: Severity) -> int:
    return {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}.get(sev.value, 0)


def _sort_key(f: Finding) -> tuple[int, float]:
    return _severity_rank(f.severity), f.confidence


__all__ = ["FindingPipeline"]
