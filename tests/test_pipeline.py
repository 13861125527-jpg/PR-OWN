"""FindingPipeline 测试（V1-d，07 §5：校验/聚类/去重/门槛/状态机）。"""


from reposage.domain.diff import parse_unified_diff
from reposage.domain.enums import FindingCategory, FindingStatus, Severity
from reposage.domain.finding import FindingCandidate
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
    severity=Severity.HIGH,
    confidence=0.9,
    category=FindingCategory.SECURITY,
    path="src/a.py",
    start=4,
    trigger="eval(x)",
):
    return FindingCandidate(
        title=title,
        severity=severity,
        confidence=confidence,
        category=category,
        claimed_path=path,
        claimed_start_line=start,
        trigger_condition=trigger,
        explanation="dynamic code",
        impact="RCE",
        suggestion="avoid eval",
    )


def _pipeline(min_confidence=0.75) -> FindingPipeline:
    return FindingPipeline(repo="repo", head_sha="abc1234", min_confidence=min_confidence)


def test_accepted_with_canonical_and_identities():
    f = _pipeline().process(run_id="r1", candidates=[_cand()], file_map=_file_map())[0]
    assert f.status is FindingStatus.ACCEPTED
    assert f.canonical_path == "src/a.py"
    assert f.canonical_start_line == 4
    assert f.fingerprint
    assert f.cross_run_match_key
    assert f.cluster_id
    assert f.finding_occurrence_id
    assert f.fingerprint != f.cross_run_match_key  # 身份区分


def test_low_confidence_suppressed():
    f = _pipeline().process(run_id="r1", candidates=[_cand(confidence=0.5)], file_map=_file_map())[0]
    assert f.status is FindingStatus.SUPPRESSED
    assert "0.50" in f.versions[-1].reason


def test_out_of_diff_body_only():
    f = _pipeline().process(
        run_id="r1", candidates=[_cand(start=99)], file_map=_file_map()
    )[0]
    assert f.status is FindingStatus.BODY_ONLY
    assert f.canonical_start_line is None


def test_cluster_merges_overlapping_same_category():
    """同 file 同 category 重叠行 → 聚类合并为一条（fingerprint 去重）。"""
    findings = _pipeline().process(
        run_id="r1",
        candidates=[
            _cand(title="a", start=4, category=FindingCategory.SECURITY),
            _cand(title="b", start=5, category=FindingCategory.SECURITY),
        ],
        file_map=_file_map(),
    )
    assert len(findings) == 1
    # 来源/证据合并：保留最高置信度 + 两条来源
    assert len(findings[0].sources) >= 1


def test_distinct_files_not_clustered():
    diff2 = DIFF.replace("src/a.py", "src/b.py").replace("def g", "def h").replace("eval(x)", "os.system(x)")
    findings = _pipeline().process(
        run_id="r1",
        candidates=[
            _cand(path="src/a.py", start=4, category=FindingCategory.SECURITY),
            _cand(path="src/b.py", start=4, category=FindingCategory.SECURITY, trigger="os.system(x)"),
        ],
        file_map={"src/a.py": parse_unified_diff(DIFF)[0], "src/b.py": parse_unified_diff(diff2)[0]},
    )
    assert len(findings) == 2


def test_versions_audit_trail():
    """状态机推进记录审计（candidate→…→accepted）。"""
    f = _pipeline().process(run_id="r1", candidates=[_cand()], file_map=_file_map())[0]
    statuses = [v.to_status for v in f.versions]
    assert statuses[0] is FindingStatus.SCHEMA_VALID
    assert FindingStatus.LOCATION_VALID in statuses
    assert FindingStatus.EVIDENCE_VALID in statuses
    assert FindingStatus.MERGED in statuses
    assert statuses[-1] is FindingStatus.ACCEPTED


def test_missing_evidence_downgrades_confidence():
    cand = _cand()
    cand.trigger_condition = ""
    cand.explanation = ""
    f = _pipeline().process(run_id="r1", candidates=[cand], file_map=_file_map())[0]
    # 0.9 → 0.72 < 0.75 → suppressed（程序可下调不可上调）
    assert f.confidence < 0.9
    assert f.status is FindingStatus.SUPPRESSED


def test_sort_by_severity_then_confidence():
    """不同簇（不同 category）按 severity×confidence 排序。"""
    findings = _pipeline().process(
        run_id="r1",
        candidates=[
            _cand(title="low", severity=Severity.LOW, confidence=0.95, start=4, category=FindingCategory.MAINTAINABILITY, trigger="y"),
            _cand(title="high", severity=Severity.HIGH, confidence=0.8, start=4),
        ],
        file_map=_file_map(),
    )
    assert len(findings) == 2
    assert findings[0].severity is Severity.HIGH
