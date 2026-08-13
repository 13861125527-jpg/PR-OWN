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


def test_finding_illegal_transition_raises():
    """P1-2：非法转换（candidate -> published）抛 FindingStateError。"""
    from reposage.domain.finding import FindingStateError

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
    with pytest.raises(FindingStateError):
        finding.record_transition(FindingStatus.PUBLISHED)
    # 状态未变
    assert finding.status is FindingStatus.CANDIDATE


def test_finding_terminal_state_not_transitionable():
    """P1-2：终态 published/suppressed 不可继续转换。"""
    from reposage.domain.finding import FindingStateError

    published = Finding(
        finding_occurrence_id="occ-2",
        run_id="run-1",
        fingerprint="fp",
        cross_run_match_key="key",
        title="t",
        severity=Severity.HIGH,
        confidence=0.9,
        category=FindingCategory.SECURITY,
        status=FindingStatus.PUBLISHED,
    )
    with pytest.raises(FindingStateError):
        published.record_transition(FindingStatus.SCHEMA_VALID)

    suppressed = published.model_copy(update={"status": FindingStatus.SUPPRESSED})
    with pytest.raises(FindingStateError):
        suppressed.record_transition(FindingStatus.MERGED)


def test_finding_publish_failed_can_retry():
    """P1-2：publish_failed -> published 是唯一允许的终态例外。"""
    finding = Finding(
        finding_occurrence_id="occ-3",
        run_id="run-1",
        fingerprint="fp",
        cross_run_match_key="key",
        title="t",
        severity=Severity.HIGH,
        confidence=0.9,
        category=FindingCategory.SECURITY,
        status=FindingStatus.PUBLISH_FAILED,
    )
    finding.record_transition(FindingStatus.PUBLISHED)
    assert finding.status is FindingStatus.PUBLISHED


def test_review_run_status_enum():
    assert ReviewRunStatus.PARTIAL.value == "partial"


def test_change_request_github_requires_external_id():
    """P2-1：github_pr 缺 external_id 拒绝。"""
    from reposage.domain.enums import ChangeRequestSource
    from reposage.domain.models import ChangeRequest, CommitRef

    with pytest.raises(ValueError):
        ChangeRequest(
            source=ChangeRequestSource.GITHUB_PR,
            base=CommitRef(sha="a" * 7, label="base"),
            head=CommitRef(sha="b" * 7, label="head"),
        )


def test_change_request_local_forbids_external_id():
    from reposage.domain.enums import ChangeRequestSource
    from reposage.domain.models import ChangeRequest, CommitRef

    with pytest.raises(ValueError):
        ChangeRequest(
            source=ChangeRequestSource.LOCAL_RANGE,
            external_id="1",
            base=CommitRef(sha="a" * 7, label="base"),
            head=CommitRef(sha="b" * 7, label="head"),
        )


def test_change_request_require_head_locked():
    from reposage.domain.enums import ChangeRequestSource
    from reposage.domain.models import ChangeRequest, CommitRef

    req = ChangeRequest(
        source=ChangeRequestSource.GITHUB_PR,
        external_id="1",
        base=CommitRef(sha="a" * 7, label="base"),
        head=CommitRef(sha="b" * 7, label="head"),
    )
    with pytest.raises(ValueError):
        req.require_head_locked()
    req.lock_head().require_head_locked()  # 锁定后通过


def test_diff_line_added_requires_new_ln():
    """P2-1：added 行必须有 new_ln。"""
    from reposage.domain.enums import DiffLineType
    from reposage.domain.models import DiffLine

    with pytest.raises(ValueError):
        DiffLine(type=DiffLineType.ADDED, new_ln=None)
    with pytest.raises(ValueError):
        DiffLine(type=DiffLineType.REMOVED, old_ln=None)


def test_repository_ref_source_fields():
    """P2-1：github 必须 owner/name；local 必须 local_path。"""
    from reposage.domain.models import RepositoryRef

    with pytest.raises(ValueError):
        RepositoryRef(provider="github")
    with pytest.raises(ValueError):
        RepositoryRef(provider="local")
    RepositoryRef(provider="github", owner="o", name="n")  # ok
    RepositoryRef(provider="local", local_path="C:/repo")  # ok


def test_global_budget_can_admit_tokens():
    """P2（复验）：Token 维度 admission control。"""
    from reposage.domain.models import GlobalBudget

    b = GlobalBudget(max_total_tokens=100, max_cost_usd=1.0)
    assert b.can_admit(input_tokens=60, max_output_tokens=30)  # 90 ≤ 100
    assert not b.can_admit(input_tokens=80, max_output_tokens=30)  # 110 > 100


def test_global_budget_can_admit_cost():
    """P2（复验）：费用估算维度 admission control。"""
    from reposage.domain.models import GlobalBudget, ModelUsage

    b = GlobalBudget(max_total_tokens=1000, max_cost_usd=1.0)
    assert b.can_admit(input_tokens=10, max_output_tokens=10, est_cost_usd=0.9)
    assert not b.can_admit(input_tokens=10, max_output_tokens=10, est_cost_usd=1.1)
    # 已消费后费用余量收紧
    b.consume(ModelUsage(model="m", role="r", input_tokens=10, output_tokens=10, cost_usd=0.6))
    assert not b.can_admit(input_tokens=10, max_output_tokens=10, est_cost_usd=0.5)  # 0.6+0.5 > 1.0
