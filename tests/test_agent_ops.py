"""V3-D Agent 运行指标分母（26 §5 / T2）。"""

from reposage.domain.enums import EvidenceKind, FindingCategory, FindingStatus, Severity
from reposage.domain.finding import Finding
from reposage.domain.models import Evidence
from reposage.evals.agent_ops import (
    failure_rate,
    groundedness,
    is_control_tool,
    repeat_rate,
    tool_effectiveness,
    tool_groundedness,
)


def _finding(
    *, status: FindingStatus, tool_call_id: str | None = None, path: str = "src/a.py", line: int = 2
) -> Finding:
    evidence = []
    if tool_call_id is not None:
        evidence.append(
            Evidence(
                kind=EvidenceKind.TOOL_RESULT,
                location="tool:read_file:x",
                tool_call_id=tool_call_id,
            )
        )
    return Finding(
        finding_occurrence_id="occ",
        run_id="run",
        fingerprint="fp",
        cross_run_match_key="key",
        title="t",
        severity=Severity.HIGH,
        confidence=0.9,
        category=FindingCategory.SECURITY,
        canonical_path=path,
        canonical_start_line=line,
        status=status,
        evidence=evidence,
    )


def test_effectiveness_and_repeat_denominators():
    assert tool_effectiveness(successful_tools=1, tool_attempts=0) == 1.0
    assert tool_effectiveness(successful_tools=1, tool_attempts=4) == 0.25
    assert repeat_rate(repeat_count=0, tool_attempts=0) == 0.0
    assert repeat_rate(repeat_count=3, tool_attempts=4) == 0.75
    assert is_control_tool("submit_finding")
    assert is_control_tool("finish_review")
    assert not is_control_tool("read_file")


def test_failure_rate_none_when_no_tasks():
    assert failure_rate(failed_or_partial=0, task_count=0) is None
    assert failure_rate(failed_or_partial=1, task_count=4) == 0.25


def test_groundedness_none_when_no_accepted():
    pending = _finding(status=FindingStatus.MERGED, tool_call_id="tc-1")
    assert groundedness([pending], tool_call_ids={"tc-1"}, diff_locations=set()) is None


def test_groundedness_tool_or_diff():
    via_tool = _finding(status=FindingStatus.ACCEPTED, tool_call_id="tc-1")
    via_diff = _finding(status=FindingStatus.ACCEPTED, tool_call_id=None, line=4)
    assert groundedness([via_tool], tool_call_ids={"tc-1"}, diff_locations=set()) == 1.0
    assert groundedness([via_diff], tool_call_ids=set(), diff_locations={("src/a.py", 4)}) == 1.0
    assert groundedness([via_diff], tool_call_ids=set(), diff_locations=set()) == 0.0
    assert tool_groundedness([via_diff], tool_call_ids=set()) == 0.0
    assert tool_groundedness([via_tool], tool_call_ids={"tc-1"}) == 1.0
