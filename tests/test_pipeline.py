"""FindingPipeline 测试（V1-d，07 §5：校验/聚类/去重/门槛/状态机）。"""


from reposage.domain.diff import parse_unified_diff
from reposage.domain.enums import EvidenceKind, FindingCategory, FindingStatus, Severity
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


async def _process(*, pipeline=None, **kwargs):
    p = pipeline or _pipeline()
    return (await p.process(**kwargs)).findings


async def test_role_id_becomes_llm_role_source():
    cand = _cand()
    cand.role_id = "security"
    f = (await _process(run_id="r1", candidates=[cand], file_map=_file_map()))[0]
    assert f.sources[0].kind.value == "llm_role"
    assert f.sources[0].role_id == "security"
    f = (await _process(run_id="r1", candidates=[_cand()], file_map=_file_map()))[0]
    assert f.status is FindingStatus.ACCEPTED
    assert f.canonical_path == "src/a.py"
    assert f.canonical_start_line == 4
    assert f.fingerprint
    assert f.cross_run_match_key
    assert f.cluster_id
    assert f.finding_occurrence_id
    assert f.fingerprint != f.cross_run_match_key  # 身份区分


async def test_low_confidence_suppressed():
    f = (await _process(run_id="r1", candidates=[_cand(confidence=0.5)], file_map=_file_map()))[0]
    assert f.status is FindingStatus.SUPPRESSED
    assert "0.50" in f.versions[-1].reason


async def test_out_of_diff_body_only():
    f = (await _process(
        run_id="r1", candidates=[_cand(start=99)], file_map=_file_map()
    ))[0]
    assert f.status is FindingStatus.BODY_ONLY
    assert f.canonical_start_line is None


async def test_cluster_merges_overlapping_same_category():
    """同 file 同 category 重叠行 → 聚类合并为一条（fingerprint 去重）。"""
    findings = await _process(
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


async def test_distinct_trigger_clusters_with_same_fingerprint_are_deduplicated():
    findings = await _process(
        run_id="r1",
        candidates=[_cand(trigger="first trigger"), _cand(trigger="second trigger")],
        file_map=_file_map(),
    )
    assert len(findings) == 1
    assert findings[0].fingerprint


async def test_distinct_files_not_clustered():
    diff2 = DIFF.replace("src/a.py", "src/b.py").replace("def g", "def h").replace("eval(x)", "os.system(x)")
    findings = await _process(
        run_id="r1",
        candidates=[
            _cand(path="src/a.py", start=4, category=FindingCategory.SECURITY),
            _cand(path="src/b.py", start=4, category=FindingCategory.SECURITY, trigger="os.system(x)"),
        ],
        file_map={"src/a.py": parse_unified_diff(DIFF)[0], "src/b.py": parse_unified_diff(diff2)[0]},
    )
    assert len(findings) == 2


async def test_versions_audit_trail():
    """状态机推进记录审计（candidate→…→accepted）。"""
    f = (await _process(run_id="r1", candidates=[_cand()], file_map=_file_map()))[0]
    statuses = [v.to_status for v in f.versions]
    assert statuses[0] is FindingStatus.SCHEMA_VALID
    assert FindingStatus.LOCATION_VALID in statuses
    assert FindingStatus.EVIDENCE_VALID in statuses
    assert FindingStatus.MERGED in statuses
    assert statuses[-1] is FindingStatus.ACCEPTED


async def test_missing_evidence_auto_filled_from_real_line():
    """P1-1 验收 6：无证据但位置可定位 → 程序补真实行证据 → 正常通过（置信度不降）。"""
    cand = _cand()
    cand.trigger_condition = ""
    cand.explanation = ""
    f = (await _process(run_id="r1", candidates=[cand], file_map=_file_map()))[0]
    assert f.confidence == 0.9  # 无降级
    assert f.status is FindingStatus.ACCEPTED
    assert f.evidence and f.evidence[0].verified is True
    assert f.evidence[0].content == "def g():"  # 真实新增行（canonical 行 4）


async def test_sort_by_severity_then_confidence():
    """不同簇（不同 category）按 severity×confidence 排序。"""
    findings = await _process(
        run_id="r1",
        candidates=[
            _cand(title="low", severity=Severity.LOW, confidence=0.95, start=4, category=FindingCategory.MAINTAINABILITY, trigger="y"),
            _cand(title="high", severity=Severity.HIGH, confidence=0.8, start=4),
        ],
        file_map=_file_map(),
    )
    assert len(findings) == 2
    assert findings[0].severity is Severity.HIGH
# ================= V1-d 返工：P1-2 证据信任 / P1-3 unknown path =================


async def test_unknown_path_suppressed():
    """P1-3：幻觉路径 → SUPPRESSED（不作为正文结论）。"""
    f = (await _process(
        run_id="r1", candidates=[_cand(path="src/ghost.py")], file_map=_file_map()
    ))[0]
    assert f.status is FindingStatus.SUPPRESSED
    assert "不在本次变更文件" in f.versions[-1].reason


async def test_evidence_content_from_real_diff_line():
    """P1-2：程序补的证据内容来自真实新增行（verified=True），不是模型描述。"""
    f = (await _process(run_id="r1", candidates=[_cand(start=5)], file_map=_file_map()))[0]
    assert f.evidence
    ev = f.evidence[0]
    assert ev.verified is True
    assert ev.content == "    return eval(x)"  # 真实新增行文本（不含 + 前缀）


async def test_evidence_forged_by_model_not_verified():
    """P1-2：模型提交伪造 content + verified=True 不能穿透（程序重判为 False → 降级）。"""
    from reposage.domain.models import Evidence

    cand = _cand()
    cand.evidence = [
        Evidence(
            kind=EvidenceKind.DIFF_LINE,
            location="src/a.py:5",
            content="return safe_code()",  # 与真实新增行不一致
            verified=True,  # 模型伪造
        )
    ]
    f = (await _process(run_id="r1", candidates=[cand], file_map=_file_map()))[0]
    assert f.evidence
    assert f.evidence[0].verified is False  # 程序重判
    # P1-1：有证据但无法验证（伪造）→ 不自动补 → 置信度下调（0.9→0.72 < 0.75 → suppressed）
    assert f.confidence < 0.9
    assert f.status is FindingStatus.SUPPRESSED


async def test_evidence_matching_real_line_verified():
    """P1-2：模型提交内容与真实新增行一致 → 程序验证 verified=True。"""
    from reposage.domain.models import Evidence

    cand = _cand(start=5)
    cand.evidence = [
        Evidence(
            kind=EvidenceKind.DIFF_LINE,
            location="src/a.py:5",
            content="    return eval(x)",  # 与真实新增行（行 5）一致
            verified=False,  # 模型没声称已验证
        )
    ]
    f = (await _process(run_id="r1", candidates=[cand], file_map=_file_map()))[0]
    assert f.evidence[0].verified is True


async def test_program_stamped_agent_tool_evidence_remains_verified():
    """受控 Agent loop 的成功 tool_call_id 可作为跨文件证据进入 Judge。"""
    from reposage.domain.enums import FindingSourceKind
    from reposage.domain.models import Evidence

    cand = _cand(start=5)
    cand.source_kind = FindingSourceKind.TOOL_AGENT
    cand.evidence = [
        Evidence(
            kind=EvidenceKind.TOOL_RESULT,
            location="read_file:lib/helper.py:L1-20",
            content="",
            tool_call_id="tc-read-helper",
            verified=True,
        )
    ]
    f = (await _process(run_id="r1", candidates=[cand], file_map=_file_map()))[0]
    assert f.evidence[0].verified is True
    assert f.status is FindingStatus.ACCEPTED


async def test_non_agent_cannot_self_verify_tool_evidence():
    """普通模型伪造 TOOL_RESULT verified 标记仍会被清零。"""
    from reposage.domain.models import Evidence

    cand = _cand(start=5)
    cand.evidence = [
        Evidence(
            kind=EvidenceKind.TOOL_RESULT,
            location="read_file:lib/helper.py:L1-20",
            content="",
            tool_call_id="made-up",
            verified=True,
        )
    ]
    f = (await _process(run_id="r1", candidates=[cand], file_map=_file_map()))[0]
    assert f.evidence[0].verified is False
    assert f.status is FindingStatus.SUPPRESSED


def _static_cand(*, rule="B006", start=5, category=FindingCategory.CORRECTNESS, path="src/a.py"):
    from reposage.domain.enums import FindingSourceKind
    from reposage.domain.models import Evidence

    return FindingCandidate(
        title=f"ruff {rule}",
        severity=Severity.MEDIUM,
        confidence=1.0,
        category=category,
        claimed_path=path,
        claimed_start_line=start,
        claimed_end_line=start,
        trigger_condition=f"ruff:{rule}",
        explanation="static",
        evidence=[
            Evidence(
                kind=EvidenceKind.STATIC_RESULT,
                location=f"{path}:{start}",
                content="static",
                verified=False,
            )
        ],
        source_kind=FindingSourceKind.STATIC_ANALYZER,
        analyzer_id="ruff",
        rule_id=f"ruff:{rule}",
    )


async def test_static_same_rule_deduped():
    findings = await _process(
        run_id="r1",
        candidates=[_static_cand(), _static_cand()],
        file_map=_file_map(),
    )
    accepted = [f for f in findings if f.status is FindingStatus.ACCEPTED]
    assert len(accepted) == 1
    assert accepted[0].sources[0].kind.value == "static_analyzer"
    assert accepted[0].fingerprint


async def test_static_distinct_rules_not_merged():
    findings = await _process(
        run_id="r1",
        candidates=[
            _static_cand(rule="B006", category=FindingCategory.CORRECTNESS),
            _static_cand(rule="S110", category=FindingCategory.SECURITY),
        ],
        file_map=_file_map(),
    )
    accepted = [f for f in findings if f.status is FindingStatus.ACCEPTED]
    assert len(accepted) == 2


async def test_llm_and_static_fuse_sources():
    from reposage.domain.enums import FindingSourceKind

    findings = await _process(
        run_id="r1",
        candidates=[
            _cand(start=5, category=FindingCategory.SECURITY, confidence=1.0),
            _static_cand(rule="S110", start=5, category=FindingCategory.SECURITY),
        ],
        file_map=_file_map(),
    )
    accepted = [f for f in findings if f.status is FindingStatus.ACCEPTED]
    assert len(accepted) == 1
    kinds = {s.kind for s in accepted[0].sources}
    assert FindingSourceKind.LLM_GENERAL in kinds
    assert FindingSourceKind.STATIC_ANALYZER in kinds
    assert any(ev.kind is EvidenceKind.STATIC_RESULT for ev in accepted[0].evidence)
    assert accepted[0].title == "eval"  # DP-8b：并列保留 LLM 文案
    assert accepted[0].rule_id == "ruff:S110"
    llm_only = (await _process(
        run_id="r1", candidates=[_cand(start=5, category=FindingCategory.SECURITY, confidence=1.0)],
        file_map=_file_map(),
    ))[0]
    assert accepted[0].fingerprint != llm_only.fingerprint


async def test_llm_does_not_transitively_fuse_distinct_static_rules():
    """B2：一条 LLM 不能把两个不同静态 rule_id 并成 model key。"""
    findings = await _process(
        run_id="r1",
        candidates=[
            _cand(start=5, category=FindingCategory.SECURITY, confidence=1.0),
            _static_cand(rule="S307", start=5, category=FindingCategory.SECURITY),
            _static_cand(rule="S110", start=4, category=FindingCategory.SECURITY),
        ],
        file_map=_file_map(),
    )
    accepted = [f for f in findings if f.status is FindingStatus.ACCEPTED]
    assert len(accepted) == 2
    assert {f.rule_id for f in accepted} == {"ruff:S307", "ruff:S110"}
    llm_only = (await _process(
        run_id="r1",
        candidates=[_cand(start=5, category=FindingCategory.SECURITY, confidence=1.0)],
        file_map=_file_map(),
    ))[0]
    assert all(f.fingerprint != llm_only.fingerprint for f in accepted)


async def test_model_cannot_impersonate_static_analyzer():
    from reposage.domain.enums import FindingSourceKind
    from reposage.domain.models import Evidence

    cand = _cand(start=5)
    cand.source_kind = FindingSourceKind.STATIC_ANALYZER
    cand.analyzer_id = None
    cand.rule_id = "ruff:B006"
    cand.evidence = [
        Evidence(kind=EvidenceKind.STATIC_RESULT, location="src/a.py:5", content="fake", verified=True)
    ]
    f = (await _process(run_id="r1", candidates=[cand], file_map=_file_map()))[0]
    assert all(s.kind is not FindingSourceKind.STATIC_ANALYZER for s in f.sources)
    assert all(ev.kind is not EvidenceKind.STATIC_RESULT for ev in f.evidence)


def test_duplicate_survival_rate_helper():
    from reposage.review.pipeline import compute_duplicate_survival_rate

    assert compute_duplicate_survival_rate(4, 1) == 0.25
    assert compute_duplicate_survival_rate(0, 0) is None
    assert compute_duplicate_survival_rate(5, 1) == 0.2


def test_fingerprint_empty_before_merge():
    p = _pipeline()
    located = p._validate_and_locate("r1", _cand(), _file_map())
    assert located.fingerprint == ""
    assert located.cross_run_match_key == ""
    merged = p._merge_cluster("r1", [located], _file_map())
    assert merged is not None
    assert merged.fingerprint
    assert merged.cross_run_match_key
    assert merged.fingerprint != merged.cross_run_match_key
