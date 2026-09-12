"""V3-D Agent 运行指标（26 §5）。只读聚合，不改 Pipeline。"""

from __future__ import annotations

from reposage.domain.enums import FindingStatus
from reposage.domain.finding import Finding
from reposage.domain.models import Evidence

_CONTROL = frozenset({"submit_finding", "finish_review"})


def tool_effectiveness(*, successful_tools: int, tool_attempts: int) -> float:
    """成功只读工具 / attempts；控制工具不计。"""
    return successful_tools / max(tool_attempts, 1)


def repeat_rate(*, repeat_count: int, tool_attempts: int) -> float:
    """repeat 观察次数 / attempts。"""
    return repeat_count / max(tool_attempts, 1)


def failure_rate(*, failed_or_partial: int, task_count: int) -> float | None:
    if task_count <= 0:
        return None
    return failed_or_partial / task_count


def is_control_tool(name: str) -> bool:
    return name in _CONTROL


def groundedness(
    findings: list[Finding],
    *,
    tool_call_ids: set[str],
    diff_locations: set[tuple[str, int]],
) -> float | None:
    """accepted 中可追溯到 tool_call 或 diff 行的比例；0 条 accepted → None。"""
    accepted = [item for item in findings if item.status is FindingStatus.ACCEPTED]
    if not accepted:
        return None
    hits = 0
    for finding in accepted:
        if _tool_grounded(finding.evidence, tool_call_ids) or _diff_grounded(
            finding, diff_locations
        ):
            hits += 1
    return hits / len(accepted)


def _tool_grounded(evidence: list[Evidence], tool_call_ids: set[str]) -> bool:
    return any(
        item.tool_call_id is not None and item.tool_call_id in tool_call_ids for item in evidence
    )


def tool_groundedness(findings: list[Finding], *, tool_call_ids: set[str]) -> float | None:
    """只认 tool_call_id；不把 diff 行当已追溯。0 条 accepted → None。"""
    return groundedness(findings, tool_call_ids=tool_call_ids, diff_locations=set())


def _diff_grounded(finding: Finding, diff_locations: set[tuple[str, int]]) -> bool:
    path = finding.canonical_path
    line = finding.canonical_start_line
    if path is None or line is None:
        return False
    return (path, line) in diff_locations
