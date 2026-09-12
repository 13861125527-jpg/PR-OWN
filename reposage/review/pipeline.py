"""统一 FindingPipeline——Finding 正式生命周期唯一所有者（review/pipeline.py，07 §5 / 05 §3）。

ReviewService 编排；任何 Strategy 只返回 FindingCandidate + SourceRunResult，
本流水线执行：schema 校验 → location 校验（claimed→canonical 重定位）→ evidence
校验 → 聚类 → 去重（fingerprint）→ 来源合并 → 反馈压制 → Judge（可插拔）→ 门槛。

铁律（P0-1 / 05 §4）：canonical 事实字段、fingerprint、cluster_id、status、sources
仅程序写入；Pipeline 不修改 canonical 事实，只验证与取舍。不直接写 SQLite。
"""

from __future__ import annotations

import asyncio
import uuid

from pydantic import BaseModel, Field

from ..domain.enums import EvidenceKind, FindingSourceKind, FindingStatus, Severity
from ..domain.finding import (
    Finding,
    FindingCandidate,
    compute_cross_run_match_key,
    compute_fingerprint,
)
from ..domain.models import (
    ChangedFile,
    CoverageItem,
    Evidence,
    FindingSource,
    GlobalBudget,
    ModelUsage,
)
from ..domain.run import FeedbackMemory, ReviewTask
from .feedback import apply_feedback_suppressions
from .judge import FindingAdjudicator, apply_judge_decisions, judge_skipped_item
from .location import (
    LOCATION_VALID,
    UNKNOWN_PATH,
    LocationResolution,
    added_line_content,
    added_line_numbers,
    resolve_location,
)

# 聚类行区间重叠容差（同 file + 同 category + 行区间邻近 → 同一问题）
_CLUSTER_LINE_TOLERANCE = 8

# 模型候选无规则键时的规则键归一（参与 fingerprint / cross_run_match_key）
_MODEL_RULE_KEY = "model"


class PipelineMetrics(BaseModel):
    raw_candidates: int = 0
    merged: int = 0
    judge_enabled: bool = False
    judge_keep: int = 0
    judge_downrank: int = 0
    needs_evidence: int = 0
    duplicate_survival_rate: float | None = None
    dedup_collapse_rate: float | None = None
    feedback_suppressed: int = 0
    feedback_l4_injected: int = 0
    feedback_l4_dropped: int = 0


class PipelineResult(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    tasks: list[ReviewTask] = Field(default_factory=list)
    usages: list[ModelUsage] = Field(default_factory=list)
    coverage_items: list[CoverageItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metrics: PipelineMetrics = Field(default_factory=PipelineMetrics)
    partial: bool = False


def compute_duplicate_survival_rate(n_raw: int, n_merged: int) -> float | None:
    """n_merged / n_raw；n_raw=0 则不定义。"""
    if n_raw <= 0:
        return None
    return n_merged / n_raw


class FindingPipeline:
    """候选 → 正式 Finding（唯一生命周期推进者）。"""

    def __init__(
        self,
        *,
        repo: str,
        head_sha: str,
        min_confidence: float = 0.75,
        judge_enabled: bool = False,
        max_findings: int = 32,
    ) -> None:
        self.repo = repo
        self.head_sha = head_sha
        self.min_confidence = min_confidence
        self.judge_enabled = judge_enabled
        self.max_findings = max_findings

    async def process(
        self,
        *,
        run_id: str,
        candidates: list[FindingCandidate],
        file_map: dict[str, ChangedFile],
        adjudicator: FindingAdjudicator | None = None,
        budget: GlobalBudget | None = None,
        model_semaphore: asyncio.Semaphore | None = None,
        feedback: list[FeedbackMemory] | None = None,
    ) -> PipelineResult:
        """完整流水线；返回 PipelineResult（Service 统一落库）。"""
        validated: list[Finding] = []
        body_only: list[Finding] = []
        suppressed: list[Finding] = []
        for cand in candidates:
            f = self._validate_and_locate(run_id, cand, file_map)
            f.needs_evidence = not _has_verified_evidence(f)
            if f.status is FindingStatus.BODY_ONLY:
                body_only.append(f)  # 终态：不进行内、不聚类（05 §3 状态机）
            elif f.status is FindingStatus.SUPPRESSED:
                suppressed.append(f)  # 终态（schema 非法/未知路径）：不聚类
            else:
                validated.append(f)

        clustered = self._cluster(validated)
        fused = self._fuse_cross_source(clustered)
        merged_findings: list[Finding] = []
        for cluster in fused:
            merged = self._merge_cluster(run_id, cluster, file_map)
            if merged is None:
                continue
            merged.record_transition(FindingStatus.MERGED, actor="program", reason="聚类去重保留")
            merged.needs_evidence = not _has_verified_evidence(merged)
            merged_findings.append(merged)
        merged_findings = self._dedupe_fingerprints(merged_findings)

        tasks: list[ReviewTask] = []
        usages: list[ModelUsage] = []
        coverage_items: list[CoverageItem] = []
        warnings: list[str] = []
        partial = False
        judge_keep = 0
        judge_downrank = 0
        feedback_suppressed = apply_feedback_suppressions(
            [*merged_findings, *body_only],
            feedback or [],
            repo=self.repo,
        )

        if self.judge_enabled:
            for merged in merged_findings:
                if merged.needs_evidence and merged.status is FindingStatus.MERGED:
                    merged.record_transition(
                        FindingStatus.BODY_ONLY,
                        actor="program",
                        reason="needs_evidence",
                    )
            eligible = [f for f in merged_findings if f.status is FindingStatus.MERGED]
            eligible.sort(key=_sort_key, reverse=True)
            to_judge = eligible[: self.max_findings]
            overflow = eligible[self.max_findings :]
            if overflow:
                warnings.append("judge_skipped:overflow")
                coverage_items.append(judge_skipped_item("overflow"))
                partial = True
            if to_judge and adjudicator is not None and budget is not None and model_semaphore is not None:
                adj = await adjudicator.adjudicate(
                    run_id=run_id,
                    findings=to_judge,
                    budget=budget,
                    model_semaphore=model_semaphore,
                )
                judged_ids = {f.finding_occurrence_id for f in to_judge}
                judge_keep, judge_downrank = apply_judge_decisions(
                    merged_findings, adj.output.decisions, judged_ids=judged_ids
                )
                for finding in merged_findings:
                    if finding.status is FindingStatus.MERGED:
                        self._refresh_identity(finding)
                tasks.extend(adj.tasks)
                usages.extend(adj.usages)
                coverage_items.extend(adj.coverage_items)
                warnings.extend(adj.warnings)
                if adj.partial:
                    partial = True
            elif to_judge:
                warnings.append("judge_skipped:not_configured")
                coverage_items.append(judge_skipped_item("not_configured"))
                partial = True

        for merged in merged_findings:
            if merged.status is not FindingStatus.MERGED:
                continue
            if merged.confidence >= self.min_confidence:
                merged.record_transition(FindingStatus.ACCEPTED, actor="program", reason="置信度门槛")
            else:
                merged.record_transition(
                    FindingStatus.SUPPRESSED, actor="program",
                    reason=f"confidence {merged.confidence:.2f} < {self.min_confidence}",
                )

        finalized: list[Finding] = [*body_only, *suppressed, *merged_findings]
        finalized.sort(key=_sort_key, reverse=True)  # 07 §5 排序器（severity×confidence）
        n_raw = len(candidates)
        n_merged = len(merged_findings)
        survival = compute_duplicate_survival_rate(n_raw, n_merged)
        collapse = None if survival is None else 1.0 - survival
        metrics = PipelineMetrics(
            raw_candidates=n_raw,
            merged=n_merged,
            judge_enabled=self.judge_enabled,
            judge_keep=judge_keep,
            judge_downrank=judge_downrank,
            needs_evidence=sum(1 for f in finalized if f.needs_evidence),
            duplicate_survival_rate=survival,
            dedup_collapse_rate=collapse,
            feedback_suppressed=feedback_suppressed,
        )
        return PipelineResult(
            findings=finalized,
            tasks=tasks,
            usages=usages,
            coverage_items=coverage_items,
            warnings=warnings,
            metrics=metrics,
            partial=partial,
        )

    @staticmethod
    def _dedupe_fingerprints(findings: list[Finding]) -> list[Finding]:
        """Fingerprint 是 run 内唯一身份；语义触发文案不同也不能重复落库。"""
        by_fingerprint: dict[str, Finding] = {}
        for finding in findings:
            previous = by_fingerprint.get(finding.fingerprint)
            if previous is None:
                by_fingerprint[finding.fingerprint] = finding
                continue
            keeper, extra = (
                (finding, previous) if finding.confidence > previous.confidence else (previous, finding)
            )
            for evidence in extra.evidence:
                if evidence not in keeper.evidence:
                    keeper.evidence.append(evidence)
            for source in extra.sources:
                if source not in keeper.sources:
                    keeper.sources.append(source)
            by_fingerprint[finding.fingerprint] = keeper
        return list(by_fingerprint.values())

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
            sources=[_source_from_candidate(cand)],
            is_outside_diff=cand.is_outside_diff,
            rule_id=cand.rule_id if _authentic_static(cand) else None,
        )

        if not _authentic_static(cand):
            f.evidence = [ev for ev in f.evidence if ev.kind is not EvidenceKind.STATIC_RESULT]

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
        static = _is_static_finding(f)
        added = set()
        if f.canonical_path and f.canonical_path in file_map:
            added = added_line_numbers(file_map[f.canonical_path])
        for ev in f.evidence:
            was_verified = ev.verified
            ev.verified = False  # 先清零，程序重判
            if (
                ev.kind is EvidenceKind.TOOL_RESULT
                and was_verified
                and ev.tool_call_id
                and any(source.kind is FindingSourceKind.TOOL_AGENT for source in f.sources)
            ):
                # TOOL_AGENT 来源由受控 Agent loop 盖戳；其 evidence_index 只会在
                # 工具成功并持久化 tool_call_id 后写入。普通模型候选无法借此自证。
                ev.verified = True
            elif ev.kind is EvidenceKind.DIFF_LINE:
                path, line = _parse_evidence_location(ev.location)
                file = file_map.get(path) if path else None
                if file is not None and line is not None:
                    real = added_line_content(file, line)
                    if real is not None and ev.content == real:
                        ev.verified = True  # 内容与真实新增行一致 → 程序验证
            elif (
                ev.kind is EvidenceKind.STATIC_RESULT
                and static
                and f.canonical_start_line is not None
                and f.canonical_start_line in added
            ):
                ev.verified = True
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

    @classmethod
    def _fuse_cross_source(cls, clusters: list[list[Finding]]) -> list[list[Finding]]:
        """LLM 与静态：同 path + category + 行重叠则合成一簇（DP-8）。"""
        merged: list[list[Finding]] = [list(c) for c in clusters]
        changed = True
        while changed:
            changed = False
            for i in range(len(merged)):
                for j in range(i + 1, len(merged)):
                    if cls._clusters_fuseable(merged[i], merged[j]):
                        merged[i].extend(merged[j])
                        del merged[j]
                        changed = True
                        break
                if changed:
                    break
        return merged

    @staticmethod
    def _clusters_fuseable(left: list[Finding], right: list[Finding]) -> bool:
        keys = _static_rule_keys(left) | _static_rule_keys(right)
        if len(keys) > 1:
            return False
        for a in left:
            for b in right:
                if _can_fuse_pair(a, b):
                    return True
        return False

    # ---- 阶段 3：合并 + fingerprint 去重 + 门槛 ----

    def _merge_cluster(
        self, run_id: str, cluster: list[Finding], file_map: dict[str, ChangedFile]
    ) -> Finding | None:
        """保留最高置信度，合并证据/来源；聚类后计算 fingerprint 去重。"""
        rule_key = _cluster_rule_key(cluster)
        merged = _pick_survivor(cluster)
        merged.evidence = _merge_evidence(cluster, file_map)
        merged.sources = _merge_sources(cluster)
        merged.confidence = max(f.confidence for f in cluster)
        if rule_key != _MODEL_RULE_KEY:
            merged.rule_id = rule_key

        line_anchor = merged.canonical_start_line
        fingerprint = compute_fingerprint(
            repo=self.repo,
            head_sha=self.head_sha,
            canonical_path=merged.canonical_path,
            line_anchor=line_anchor,
            category=merged.category,
            rule_or_issue_key=rule_key,
        )
        cross_key = compute_cross_run_match_key(
            repo=self.repo,
            canonical_path=merged.canonical_path,
            symbol_or_anchor=str(line_anchor) if line_anchor is not None else None,
            category=merged.category,
            rule_or_issue_key=rule_key,
        )
        merged.fingerprint = fingerprint
        merged.cross_run_match_key = cross_key
        merged.cluster_id = uuid.uuid4().hex
        return merged

    def _refresh_identity(self, finding: Finding) -> None:
        """Recompute identity after Judge normalizes a category."""
        rule_key = finding.rule_id or _MODEL_RULE_KEY
        line_anchor = finding.canonical_start_line
        finding.fingerprint = compute_fingerprint(
            repo=self.repo,
            head_sha=self.head_sha,
            canonical_path=finding.canonical_path,
            line_anchor=line_anchor,
            category=finding.category,
            rule_or_issue_key=rule_key,
        )
        finding.cross_run_match_key = compute_cross_run_match_key(
            repo=self.repo,
            canonical_path=finding.canonical_path,
            symbol_or_anchor=str(line_anchor) if line_anchor is not None else None,
            category=finding.category,
            rule_or_issue_key=rule_key,
        )


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


def _authentic_static(cand: FindingCandidate) -> bool:
    return (
        cand.source_kind is FindingSourceKind.STATIC_ANALYZER
        and bool(cand.analyzer_id)
        and bool(cand.rule_id)
        and not cand.role_id
    )


def _source_from_candidate(cand: FindingCandidate) -> FindingSource:
    if _authentic_static(cand):
        return FindingSource(
            kind=FindingSourceKind.STATIC_ANALYZER,
            analyzer_id=cand.analyzer_id,
            confidence=cand.confidence,
            verified_by="program",
        )
    if cand.source_kind is FindingSourceKind.TOOL_AGENT:
        return FindingSource(
            kind=FindingSourceKind.TOOL_AGENT,
            confidence=cand.confidence,
            verified_by="program",
        )
    return FindingSource(
        kind=FindingSourceKind.LLM_ROLE if cand.role_id else FindingSourceKind.LLM_GENERAL,
        role_id=cand.role_id,
        confidence=cand.confidence,
    )


def _is_static_finding(f: Finding) -> bool:
    return any(src.kind is FindingSourceKind.STATIC_ANALYZER for src in f.sources)


def _line_overlap(a: Finding, b: Finding) -> bool:
    if a.canonical_start_line is None or b.canonical_start_line is None:
        return False
    a_lo, a_hi = a.canonical_start_line, a.canonical_end_line or a.canonical_start_line
    b_lo, b_hi = b.canonical_start_line, b.canonical_end_line or b.canonical_start_line
    return not (a_hi + _CLUSTER_LINE_TOLERANCE < b_lo or b_hi + _CLUSTER_LINE_TOLERANCE < a_lo)


def _can_fuse_pair(a: Finding, b: Finding) -> bool:
    if a.category is not b.category:
        return False
    path_a = a.canonical_path or a.claimed_path
    path_b = b.canonical_path or b.claimed_path
    if not path_a or path_a != path_b:
        return False
    if not _line_overlap(a, b):
        return False
    return _is_static_finding(a) != _is_static_finding(b)


def _pick_survivor(cluster: list[Finding]) -> Finding:
    best = max(f.confidence for f in cluster)
    tied = [f for f in cluster if f.confidence == best]
    llm = [f for f in tied if not _is_static_finding(f)]
    if llm:
        return llm[0]
    return tied[0]


def _cluster_rule_key(cluster: list[Finding]) -> str:
    keys = list(_static_rule_keys(cluster))
    if len(keys) == 1:
        return keys[0]
    return _MODEL_RULE_KEY


def _static_rule_keys(findings: list[Finding]) -> set[str]:
    return {f.rule_id for f in findings if f.rule_id}


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


__all__ = [
    "FindingPipeline",
    "PipelineMetrics",
    "PipelineResult",
    "compute_duplicate_survival_rate",
]
