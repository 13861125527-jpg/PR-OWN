"""V2-E 反馈记忆：校验、匹配、Pipeline 压制、L4 注入。"""

from __future__ import annotations

import pytest
from reposage.domain.diff import parse_unified_diff
from reposage.domain.enums import (
    ContextLayer,
    ContextSourceKind,
    CoverageReason,
    FeedbackKind,
    FindingCategory,
    FindingStatus,
    Severity,
)
from reposage.domain.finding import Finding, FindingCandidate
from reposage.domain.run import FeedbackMemory
from reposage.review.context import ContextAssembler, ContextBudget
from reposage.review.feedback import (
    FeedbackConfigError,
    apply_feedback_suppressions,
    l4_feedback_text,
    matches_finding,
    select_l4_feedback,
    validate_feedback_memory,
)
from reposage.review.pipeline import FindingPipeline

DIFF = """\
diff --git a/src/a.py b/src/a.py
index 1..2 100644
--- a/src/a.py
+++ b/src/a.py
@@ -1,2 +1,6 @@
 def f():
     return 1
+
+def g():
+    return eval(x)
+    return 2
"""


def _file_map():
    return {"src/a.py": parse_unified_diff(DIFF)[0]}


def _cand(
    *,
    title="eval",
    start=4,
    trigger="eval(x)",
    category=FindingCategory.SECURITY,
    confidence=0.9,
    path="src/a.py",
):
    return FindingCandidate(
        title=title,
        severity=Severity.HIGH,
        confidence=confidence,
        category=category,
        claimed_path=path,
        claimed_start_line=start,
        trigger_condition=trigger,
        explanation="dynamic code",
        impact="RCE",
        suggestion="avoid eval",
    )


def _mem(**kwargs) -> FeedbackMemory:
    data = {
        "id": 1,
        "repo": "repo",
        "kind": FeedbackKind.FALSE_POSITIVE,
        "path": "src/a.py",
        "category": "security",
    }
    data.update(kwargs)
    return FeedbackMemory.model_validate(data)


def _finding(
    *,
    line=4,
    path="src/a.py",
    category=FindingCategory.SECURITY,
    cross="key:4",
    rule_id=None,
) -> Finding:
    return Finding(
        finding_occurrence_id="occ",
        run_id="r",
        fingerprint="fp",
        cross_run_match_key=cross,
        title="eval",
        severity=Severity.HIGH,
        confidence=0.9,
        category=category,
        canonical_path=path,
        canonical_start_line=line,
        trigger_condition="eval(x)",
        explanation="dynamic",
        rule_id=rule_id,
        status=FindingStatus.MERGED,
    )


def test_repo_only_rejected():
    with pytest.raises(FeedbackConfigError, match="全局压制"):
        validate_feedback_memory(FeedbackMemory(repo="repo", kind=FeedbackKind.WONT_FIX))


def test_absolute_path_rejected():
    with pytest.raises(FeedbackConfigError):
        validate_feedback_memory(_mem(path="/etc/passwd"))


def test_parent_path_rejected():
    with pytest.raises(FeedbackConfigError):
        validate_feedback_memory(_mem(path="../x.py"))


def test_illegal_and_long_pattern_rejected():
    with pytest.raises(FeedbackConfigError, match="非法 pattern"):
        validate_feedback_memory(_mem(pattern="("))
    with pytest.raises(FeedbackConfigError, match="长度"):
        validate_feedback_memory(_mem(pattern="a" * 257))


def test_path_category_and_mismatch():
    mem = validate_feedback_memory(_mem())
    hit = _finding()
    assert matches_finding(mem, hit, repo="repo")
    miss = _finding(category=FindingCategory.CORRECTNESS)
    assert not matches_finding(mem, miss, repo="repo")


def test_code_move_same_path_category():
    mem = validate_feedback_memory(_mem(rule_key="ruff:S110"))
    moved = _finding(line=40, cross="key:40", rule_id="ruff:S110")
    assert matches_finding(mem, moved, repo="repo")


def test_cross_run_key_does_not_survive_move():
    mem = validate_feedback_memory(
        FeedbackMemory(
            id=2,
            repo="repo",
            kind=FeedbackKind.FALSE_POSITIVE,
            cross_run_match_key="key:4",
        )
    )
    moved = _finding(line=40, cross="key:40")
    assert not matches_finding(mem, moved, repo="repo")
    same = _finding(line=4, cross="key:4")
    assert matches_finding(mem, same, repo="repo")


def test_inactive_does_not_match():
    mem = _mem()
    mem.revoke()
    assert not matches_finding(mem, _finding(), repo="repo")


def test_apply_suppresses_merged_not_facts():
    finding = _finding()
    fp = finding.fingerprint
    n = apply_feedback_suppressions([finding], [_mem()], repo="repo")
    assert n == 1
    assert finding.status is FindingStatus.SUPPRESSED
    assert finding.versions[-1].actor == "user"
    assert finding.versions[-1].reason == "feedback:1:false_positive"
    assert finding.fingerprint == fp
    assert finding.canonical_path == "src/a.py"


def test_rule_kind_does_not_suppress():
    finding = _finding()
    n = apply_feedback_suppressions(
        [finding],
        [_mem(kind=FeedbackKind.RULE)],
        repo="repo",
    )
    assert n == 0
    assert finding.status is FindingStatus.MERGED


async def test_pipeline_feedback_before_judge_and_body_only():
    mem = _mem()
    pipe = FindingPipeline(repo="repo", head_sha="abc1234")
    result = await pipe.process(
        run_id="r1",
        candidates=[_cand(), _cand(start=99, title="oob")],
        file_map=_file_map(),
        feedback=[mem],
    )
    statuses = {f.title: f.status for f in result.findings}
    assert statuses["eval"] is FindingStatus.SUPPRESSED
    assert statuses["oob"] is FindingStatus.SUPPRESSED
    assert result.metrics.feedback_suppressed == 2
    for f in result.findings:
        if f.status is FindingStatus.SUPPRESSED:
            assert f.versions[-1].actor == "user"


async def test_pipeline_feedback_empty_list_no_suppress():
    pipe = FindingPipeline(repo="repo", head_sha="abc1234")
    result = await pipe.process(
        run_id="r1",
        candidates=[_cand()],
        file_map=_file_map(),
        feedback=[],
    )
    assert result.findings[0].status is FindingStatus.ACCEPTED
    assert result.metrics.feedback_suppressed == 0


def test_l4_skips_cross_run_only_and_respects_path():
    only_key = FeedbackMemory(
        id=1, repo="repo", kind=FeedbackKind.FALSE_POSITIVE, cross_run_match_key="k"
    )
    other_file = _mem(id=2, path="src/b.py")
    hit = _mem(id=3, path="src/a.py")
    wide = FeedbackMemory(id=4, repo="repo", kind=FeedbackKind.RULE, category="security")
    kept, _dropped = select_l4_feedback(
        "src/a.py",
        [only_key, other_file, hit, wide],
        repo="repo",
        limit=5,
    )
    ids = [m.id for m in kept]
    assert 1 not in ids
    assert 2 not in ids
    assert 3 in ids
    assert 4 in ids
    text = l4_feedback_text(hit)
    assert "src/a.py" not in text
    assert "kind=false_positive" in text


def test_l4_injects_into_assembler_budget_and_coverage():
    assembler = ContextAssembler(
        budget=ContextBudget(total_tokens=8000),
        feedback_repo="repo",
        max_feedback_items_per_file=5,
    )
    assembler.feedback = [_mem(id=9, rationale="user said skip")]
    from reposage.domain.enums import ChangeRequestSource
    from reposage.domain.models import ChangeRequest, CommitRef

    req = ChangeRequest(
        source=ChangeRequestSource.GITHUB_PR,
        external_id="1",
        base=CommitRef(sha="a", label="base"),
        head=CommitRef(sha="b", label="head", locked=True),
    )
    units = assembler.build_file_units(run_id="r", change_request=req, file=_file_map()["src/a.py"])
    chunks = [c for u in units for c in u.context.chunks if c.source.kind is ContextSourceKind.FEEDBACK]
    assert chunks
    assert chunks[0].layer is ContextLayer.L4
    assert "src/a.py" not in chunks[0].content
    covered = [
        i
        for u in units
        for i in u.coverage.items
        if i.target == "feedback:9" and i.reason is CoverageReason.COVERED
    ]
    assert covered
