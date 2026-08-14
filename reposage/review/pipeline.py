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
from .location import (
    LOCATION_VALID,
    UNKNOWN_PATH,
    LocationResolution,
    added_line_content,
    resolve_location,
)

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
        suppressed: list[Finding] = []
        for cand in candidates:
            f = self._validate_and_locate(run_id, cand, file_map)
            if f.status is FindingStatus.BODY_ONLY:
                body_only.append(f)  # 终态：不进行内、不聚类（05 §3 状态机）
            elif f.status is FindingStatus.SUPPRESSED:
                suppressed.append(f)  # 终态（schema 非法/未知路径）：不聚类
            else:
                validated.append(f)

        clustered = self._cluster(validated)
        finalized: list[Finding] = [*body_only, *suppressed]
        for cluster in clustered:
            merged = self._merge_cluster(run_id, cluster, file_map)
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

        # location 校验：claimed → canonical（路径不存在 → suppressed，P1-3）
        res: LocationResolution = resolve_location(cand, file_map)
        f.canonical_path = res.path
        f.canonical_start_line = res.start_line
        f.canonical_end_line = res.end_line
        if res.status == LOCATION_VALID:
            f.record_transition(FindingStatus.LOCATION_VALID, actor="program", reason=res.reason)
        elif res.status == UNKNOWN_PATH:
            f.record_transition(FindingStatus.SUPPRESSED, actor="program", reason=res.reason)
            return f  # 幻觉路径：suppressed，不作为正文结论
        else:
            f.record_transition(FindingStatus.BODY_ONLY, actor="program", reason=res.reason)
            return f  # body_only：不进 evidence/聚类（正文结论，V1-e 归入正文评论）

        # evidence 校验（P1-1/P1-2：verified 仅由程序重判；模型声称的 verified 一律忽略）
        self._verify_evidence(f, file_map)
        if not f.evidence and f.canonical_path and f.canonical_start_line is not None:
            # 候选完全无证据但 canonical 已定位 → 程序从真实 diff 行补 verified 证据
            # （验收 6）；候选提供过证据但全部无法验证（伪造）→ 不补，走降级（验收 5）
            file = file_map.get(f.canonical_path)
            real = added_line_content(file, f.canonical_start_line) if file is not None else None
            if real is not None:
                f.evidence.append(
                    Evidence(
                        kind=EvidenceKind.DIFF_LINE,
                        location=f"{f.canonical_path}:{f.canonical_start_line}",
                        content=real,
                        verified=True,  # 程序读取的真实新增行
                    )
                )
        if _has_verified_evidence(f):
            f.record_transition(FindingStatus.EVIDENCE_VALID, actor="program", reason="存在已验证证据")
        else:
            # 无已验证证据（trigger/explanation 只是线索，不能等价验证通过）→ 置信度下调
            f.confidence = round(f.confidence * 0.8, 4)
            f.record_transition(
                FindingStatus.EVIDENCE_VALID, actor="program",
                reason="无已验证证据，置信度下调",
            )
        return f

    @staticmethod
    def _schema_valid(cand: FindingCandidate) -> bool:
        """防御性 schema 校验（provider 已严格校验；此处仅再验数值范围）。"""
        return 0.0 <= cand.confidence <= 1.0

    @staticmethod
    def _verify_evidence(f: Finding, file_map: dict[str, ChangedFile]) -> None:
        """P1-2：证据事实由程序验证（模型不能自证）。

        - 候选声称的 Evidence.verified 一律清零后重判；
        - DIFF_LINE 证据：仅当 content 与 ChangedFile 新增行真实文本一致且行号
          落在新增行才 verified=True；否则保持 False（按证据缺口降级）；
        - trigger_condition/explanation 只作描述，不作为已验证证据内容。
        """
        verified: list[Evidence] = []
        for ev in f.evidence:
            ev.verified = False  # 先清零，程序重判
            if ev.kind is EvidenceKind.DIFF_LINE:
                path, line = _parse_evidence_location(ev.location)
                file = file_map.get(path) if path else None
                if file is not None and line is not None:
                    real = added_line_content(file, line)
                    if real is not None and ev.content == real:
                        ev.verified = True  # 内容与真实新增行一致 → 程序验证
            verified.append(ev)
        f.evidence = verified

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
        # 同一问题的证据：触发条件归一相同（相邻行但不同缺陷不合并，multi_defect）
        if _norm_trigger(a.trigger_condition) != _norm_trigger(b.trigger_condition):
            return False
        # 行区间重叠/邻近
        a_lo, a_hi = a.canonical_start_line, a.canonical_end_line or a.canonical_start_line
        b_lo, b_hi = b.canonical_start_line, b.canonical_end_line or b.canonical_start_line
        return not (a_hi + _CLUSTER_LINE_TOLERANCE < b_lo or b_hi + _CLUSTER_LINE_TOLERANCE < a_lo)

    # ---- 阶段 3：合并 + fingerprint 去重 + 门槛 ----

    def _merge_cluster(
        self, run_id: str, cluster: list[Finding], file_map: dict[str, ChangedFile]
    ) -> Finding | None:
        """保留最高置信度，合并证据/来源；聚类后计算 fingerprint 去重。"""
        merged = max(cluster, key=lambda f: f.confidence)
        merged.evidence = _merge_evidence(cluster, file_map)
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


def _merge_evidence(cluster: list[Finding], file_map: dict[str, ChangedFile]) -> list[Evidence]:
    merged: list[Evidence] = []
    for f in cluster:
        for ev in f.evidence:
            if ev not in merged:
                merged.append(ev)
    # 无证据时补一条程序从 Diff 行号表读取的真实新增行证据（verified=True，P1-2）
    anchor = cluster[0]
    if not merged and anchor.canonical_path and anchor.canonical_start_line is not None:
        file = file_map.get(anchor.canonical_path)
        real = added_line_content(file, anchor.canonical_start_line) if file is not None else None
        if real is not None:
            merged.append(
                Evidence(
                    kind=EvidenceKind.DIFF_LINE,
                    location=f"{anchor.canonical_path}:{anchor.canonical_start_line}",
                    content=real,
                    verified=True,  # 内容来自程序读取的真实新增行
                )
            )
        else:
            # 位置可确认但内容无法读取：verified=False（证据缺口，不伪造）
            merged.append(
                Evidence(
                    kind=EvidenceKind.DIFF_LINE,
                    location=f"{anchor.canonical_path}:{anchor.canonical_start_line}",
                    content="",
                    verified=False,
                )
            )
    return merged


def _parse_evidence_location(location: str) -> tuple[str | None, int | None]:
    """解析 "path:line" 证据定位。"""
    if not location or ":" not in location:
        return None, None
    path, _, line_str = location.rpartition(":")
    try:
        return path, int(line_str)
    except ValueError:
        return None, None


def _has_verified_evidence(f: Finding) -> bool:
    """证据校验（P1-1）：至少一条程序验证通过（verified=True）的可追溯证据。"""
    return any(ev.verified for ev in f.evidence)


def _merge_sources(cluster: list[Finding]) -> list[FindingSource]:
    merged: list[FindingSource] = []
    for f in cluster:
        for src in f.sources:
            if src not in merged:
                merged.append(src)
    return merged


def _norm_trigger(value: str) -> str:
    """触发条件归一（聚类区分不同缺陷：相邻行但 trigger 不同不合并）。"""
    return " ".join(str(value or "").strip().lower().split())


def _severity_rank(sev: Severity) -> int:
    return {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}.get(sev.value, 0)


def _sort_key(f: Finding) -> tuple[int, float]:
    return _severity_rank(f.severity), f.confidence


__all__ = ["FindingPipeline"]
