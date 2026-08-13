"""domain 模型与指纹测试。"""

import pytest
from reposage.domain.enums import (
    ChangeRequestSource,
    FindingCategory,
    FindingStatus,
    ReviewRunStatus,
    Severity,
)
from reposage.domain.finding import (
    Finding,
    compute_cross_run_match_key,
    compute_fingerprint,
)
from reposage.domain.models import ChangedFile, ChangeRequest, CommitRef


def test_changed_file_rejects_parent_path():
    with pytest.raises(ValueError):
        ChangedFile(path="../etc/passwd", status="modified")


def test_changed_file_rejects_absolute_path():
    with pytest.raises(ValueError):
        ChangedFile(path="/etc/passwd", status="modified")


def test_change_request_lock_head():
    req = ChangeRequest(
        source=ChangeRequestSource.GITHUB_PR,
        external_id="42",
        base=CommitRef(sha="a" * 7, label="base"),
        head=CommitRef(sha="b" * 7, label="head"),
    )
    assert req.head.locked is False
    locked = req.lock_head()
    assert locked.head.locked is True
    assert req.head.locked is False  # 不可变：原对象不变


def test_fingerprint_contains_head_and_anchor():
    f1 = compute_fingerprint("repo", "sha1", "src/a.py", 10, FindingCategory.SECURITY, "rule:python.sec-01")
    f2 = compute_fingerprint("repo", "sha1", "src/a.py", 11, FindingCategory.SECURITY, "rule:python.sec-01")
    f3 = compute_fingerprint("repo", "sha2", "src/a.py", 10, FindingCategory.SECURITY, "rule:python.sec-01")
    assert f1 != f2  # 行锚参与
    assert f1 != f3  # head_sha 参与


def test_cross_run_key_tolerates_line_move():
    k1 = compute_cross_run_match_key("repo", "src/a.py", "parse", FindingCategory.SECURITY, "rule:python.sec-01")
    k2 = compute_cross_run_match_key("repo", "src/a.py", "parse", FindingCategory.SECURITY, "rule:python.sec-01")
    k3 = compute_cross_run_match_key("repo", "src/a.py", "other", FindingCategory.SECURITY, "rule:python.sec-01")
    assert k1 == k2  # 不含 head_sha 与行号 → 稳定
    assert k1 != k3  # 符号参与


def test_finding_lifecycle_transitions():
    finding = Finding(
        finding_occurrence_id="occ-1",
        run_id="run-1",
        fingerprint="fp",
        cross_run_match_key="key",
        title="t",
        severity=Severity.HIGH,
        confidence=0.9,
        category=FindingCategory.SECURITY,
    )
    assert finding.status is FindingStatus.CANDIDATE
    finding.record_transition(FindingStatus.SCHEMA_VALID, actor="program", reason="ok")
    assert finding.status is FindingStatus.SCHEMA_VALID
    assert finding.versions[-1].from_status is FindingStatus.CANDIDATE
    assert len(finding.versions) == 1


def test_review_run_status_enum():
    assert ReviewRunStatus.PARTIAL.value == "partial"
