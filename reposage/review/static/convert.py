"""诊断 → FindingCandidate 纯函数（V2-C §6）。不启 subprocess。"""

from __future__ import annotations

from ...domain.enums import (
    CoverageReason,
    EvidenceKind,
    FindingCategory,
    FindingSourceKind,
    Severity,
    StageName,
)
from ...domain.finding import FindingCandidate
from ...domain.models import ChangedFile, CoverageItem, Evidence, is_safe_repo_path
from ..location import added_line_content, added_line_numbers
from .protocol import AnalyzerDiagnostic

_MESSAGE_LIMIT = 500


def diagnostic_category(rule_id: str) -> FindingCategory:
    if rule_id.upper().startswith("S"):
        return FindingCategory.SECURITY
    return FindingCategory.CORRECTNESS


def diagnostic_severity(rule_id: str) -> Severity:
    if rule_id.upper().startswith("S"):
        return Severity.HIGH
    return Severity.MEDIUM


def qualified_rule_id(analyzer_id: str, rule_id: str) -> str:
    return f"{analyzer_id}:{rule_id}"


def convert_diagnostics(
    diagnostics: list[AnalyzerDiagnostic],
    file_map: dict[str, ChangedFile],
) -> tuple[list[FindingCandidate], list[CoverageItem]]:
    """把 AnalyzerDiagnostic 转为候选；非法 path / 非新增行丢弃并记 coverage。"""
    candidates: list[FindingCandidate] = []
    skipped: list[CoverageItem] = []
    for diag in diagnostics:
        analyzer_id = diag.analyzer_id
        raw_rule = diag.rule_id
        path = diag.path.replace("\\", "/")
        start_line = diag.start_line
        end_line = diag.end_line
        message = diag.message
        if not analyzer_id or not raw_rule or start_line <= 0:
            skipped.append(
                CoverageItem(
                    target=path or "<unknown>",
                    reason=CoverageReason.TRUNCATED,
                    stage=StageName.REVIEW,
                    detail="invalid diagnostic",
                )
            )
            continue
        if not is_safe_repo_path(path) or path not in file_map:
            skipped.append(
                CoverageItem(
                    target=path or "<unknown>",
                    reason=CoverageReason.TRUNCATED,
                    stage=StageName.REVIEW,
                    detail="untrusted or unknown path",
                )
            )
            continue
        file = file_map[path]
        added = added_line_numbers(file)
        if start_line not in added:
            skipped.append(
                CoverageItem(
                    target=path,
                    reason=CoverageReason.TRUNCATED,
                    stage=StageName.REVIEW,
                    detail=f"not on added line:{start_line} rule={raw_rule}",
                )
            )
            continue
        end = int(end_line) if end_line else start_line
        rule_key = qualified_rule_id(analyzer_id, raw_rule)
        line_text = added_line_content(file, start_line) or ""
        content = message[:_MESSAGE_LIMIT]
        if line_text:
            content = f"{content}\n{line_text}" if content else line_text
        candidates.append(
            FindingCandidate(
                title=f"ruff {raw_rule}",
                severity=diagnostic_severity(raw_rule),
                confidence=1.0,
                category=diagnostic_category(raw_rule),
                claimed_path=path,
                claimed_start_line=start_line,
                claimed_end_line=end,
                trigger_condition=rule_key,
                explanation=message[:_MESSAGE_LIMIT],
                impact="",
                suggestion="",
                evidence=[
                    Evidence(
                        kind=EvidenceKind.STATIC_RESULT,
                        location=f"{path}:{start_line}",
                        content=content[:_MESSAGE_LIMIT + 200],
                        verified=False,
                    )
                ],
                source_kind=FindingSourceKind.STATIC_ANALYZER,
                analyzer_id=analyzer_id,
                rule_id=rule_key,
                role_id=None,
            )
        )
    return candidates, skipped
