"""上下文构建测试（V1-b：L0–L2 + L4 内置规则 + 预算裁剪 + per-file 契约）。"""

import pytest
from reposage.domain.diff import parse_unified_diff
from reposage.domain.enums import (
    ChangeRequestSource,
    ContextLayer,
    ContextSourceKind,
    CoverageReason,
    Severity,
)
from reposage.domain.models import ChangeRequest, CommitRef
from reposage.domain.run import StageName
from reposage.review.builtin_rules import BUILTIN_RULES, BuiltinRule, match_rules
from reposage.review.context import (
    ContextAssembler,
    ContextBudget,
    build_coverage,
    estimate_tokens,
)


def _req() -> ChangeRequest:
    return ChangeRequest(
        source=ChangeRequestSource.GITHUB_PR,
        external_id="42",
        base=CommitRef(sha="a" * 7, label="base"),
        head=CommitRef(sha="b" * 7, label="head", locked=True),
        title="fix: auth bypass",
        description="move auth check before handler",
        author="alice",
        is_draft=False,
    )


def _file_from_diff(diff: str):
    return parse_unified_diff(diff)[0]


EVAL_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1..2 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,1 +1,4 @@
 import os
+def handle(data):
+    return eval(data)
+    os.system("rm -rf /")
"""

CLEAN_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1..2 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,1 +1,3 @@
 import os
+def handle(data):
+    return data.strip()
"""


# ---- token 估算 ----


def test_estimate_tokens_approx():
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd") == 1  # 4 字符 ≈ 1 token
    assert estimate_tokens("a" * 17) == 5  # ceil(17/4)


# ---- 装配与层级 ----


def test_build_context_has_l0_l1():
    ctx = ContextAssembler().build_file_context(
        run_id="run-1", change_request=_req(), file=_file_from_diff(CLEAN_DIFF)
    )
    layers = {c.layer for c in ctx.chunks}
    assert ContextLayer.L0 in layers
    assert ContextLayer.L1 in layers
    l1 = next(c for c in ctx.chunks if c.layer is ContextLayer.L1)
    assert "auth bypass" in l1.content
    assert "b" * 7 in l1.content  # head SHA


def test_build_context_l2_per_hunk():
    diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,1 +1,2 @@\n"
        " x\n"
        "+y\n"
        "@@ -5,2 +6,2 @@\n"
        " a\n"
        "-b\n"
        "+c\n"
    )
    ctx = ContextAssembler().build_file_context(
        run_id="r", change_request=_req(), file=_file_from_diff(diff)
    )
    l2 = [c for c in ctx.chunks if c.layer is ContextLayer.L2]
    assert len(l2) == 2  # 每 hunk 一块
    assert all(c.source.kind is ContextSourceKind.DIFF for c in l2)
    assert all(c.source.ref == "file:src/a.py" for c in l2)


def test_l2_large_hunk_truncated_marker():
    """06 §2：超限 hunk 分块截断并写 [TRUNCATED] 明文。"""
    big = "\n".join([f"+line {i}" for i in range(200)])
    diff = (
        "diff --git a/src/big.py b/src/big.py\n"
        "--- a/src/big.py\n"
        "+++ b/src/big.py\n"
        f"@@ -0,0 +1,{len(big.splitlines())} @@\n"
        f"{big}\n"
    )
    assembler = ContextAssembler(budget=ContextBudget(total_tokens=800, l2_ratio=0.4, reserve_ratio=0.4))
    ctx = assembler.build_file_context(run_id="r", change_request=_req(), file=_file_from_diff(diff))
    l2 = [c for c in ctx.chunks if c.layer is ContextLayer.L2]
    assert len(l2) >= 2  # 被切分
    assert any(c.truncated for c in l2)
    assert any("[TRUNCATED:" in c.content for c in l2)
    assert ctx.truncated is True


# ---- L4 内置规则 ----


def test_builtin_rules_hit_eval_and_system():
    hits = match_rules(_file_from_diff(EVAL_DIFF))
    ids = {h.rule_id for h in hits}
    assert "python.01" in ids  # eval
    assert "python.02" in ids  # os.system
    hit01 = next(h for h in hits if h.rule_id == "python.01")
    assert hit01.path == "src/app.py"
    assert hit01.line == 3  # +def handle(data) 为行2？eval 在 added 第 3 行


def test_builtin_rules_clean_no_hits():
    assert match_rules(_file_from_diff(CLEAN_DIFF)) == []


def test_builtin_rules_only_added_lines():
    """只匹配新增行：旧行含 eval 不触发（只审本次变更）。"""
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-return eval(x)\n"
        "+return x\n"
        " c\n"
    )
    assert match_rules(_file_from_diff(diff)) == []


def test_builtin_rules_non_python_skipped():
    diff = (
        "diff --git a/frontend/app.ts b/frontend/app.ts\n"
        "--- a/frontend/app.ts\n"
        "+++ b/frontend/app.ts\n"
        "@@ -1,1 +1,2 @@\n"
        " x\n"
        "+eval(y)\n"
    )
    assert match_rules(_file_from_diff(diff)) == []


def test_l4_chunks_present_for_hits():
    ctx = ContextAssembler().build_file_context(
        run_id="r", change_request=_req(), file=_file_from_diff(EVAL_DIFF)
    )
    l4 = [c for c in ctx.chunks if c.layer is ContextLayer.L4]
    assert len(l4) >= 2
    assert all(c.source.kind is ContextSourceKind.RULES for c in l4)
    assert any(c.source.ref == "rule:python.01" for c in l4)


def test_l4_absent_when_no_hits():
    ctx = ContextAssembler().build_file_context(
        run_id="r", change_request=_req(), file=_file_from_diff(CLEAN_DIFF)
    )
    assert not [c for c in ctx.chunks if c.layer is ContextLayer.L4]


def test_l4_budget_keeps_higher_severity():
    """06 §2 裁剪顺序 1：L4 预算不足时保留高 severity。"""
    rules = [
        BuiltinRule(rule_id="r.low", severity=Severity.MEDIUM, category="security", description="low", pattern=r"aaa"),
        BuiltinRule(rule_id="r.high", severity=Severity.HIGH, category="security", description="high", pattern=r"bbb"),
    ]
    diff = (
        "diff --git a/src/x.py b/src/x.py\n"
        "--- a/src/x.py\n"
        "+++ b/src/x.py\n"
        "@@ -1,1 +1,2 @@\n"
        " x\n"
        "+aaa bbb\n"
    )
    # 预算只够 1 条规则
    budget = ContextBudget(total_tokens=200, l4_ratio=0.05, reserve_ratio=0.45)
    ctx = ContextAssembler(budget=budget, rules=rules).build_file_context(
        run_id="r", change_request=_req(), file=_file_from_diff(diff)
    )
    refs = {c.source.ref for c in ctx.chunks if c.layer is ContextLayer.L4}
    assert refs == {"rule:r.high"}


# ---- 预算 / 契约 / 覆盖 ----


def test_l0_not_truncated_even_over_budget():
    """L0 不可裁（06 §2 顺序 4）：总预算极小也保留 L0。"""
    budget = ContextBudget(total_tokens=60, l0_ratio=0.5, l1_ratio=0.0, l2_ratio=0.0, l4_ratio=0.0, reserve_ratio=0.5)
    ctx = ContextAssembler(budget=budget).build_file_context(
        run_id="r", change_request=_req(), file=_file_from_diff(CLEAN_DIFF)
    )
    assert any(c.layer is ContextLayer.L0 for c in ctx.chunks)


def test_per_file_map_reduce_contract():
    """04 §1：每文件独立上下文，L2 只含自己文件。"""
    diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,1 +1,2 @@\n"
        " x\n"
        "+ya\n"
        "diff --git a/src/b.py b/src/b.py\n"
        "--- a/src/b.py\n"
        "+++ b/src/b.py\n"
        "@@ -1,1 +1,2 @@\n"
        " x\n"
        "+yb\n"
    )
    files = parse_unified_diff(diff)
    assembler = ContextAssembler()
    ctx_a = assembler.build_file_context(run_id="r", change_request=_req(), file=files[0])
    ctx_b = assembler.build_file_context(run_id="r", change_request=_req(), file=files[1])
    refs_a = {c.source.ref for c in ctx_a.chunks if c.layer is ContextLayer.L2}
    refs_b = {c.source.ref for c in ctx_b.chunks if c.layer is ContextLayer.L2}
    assert refs_a == {"file:src/a.py"}
    assert refs_b == {"file:src/b.py"}
    # L0/L1 两个上下文相同（确定性装配）
    l0_a = next(c for c in ctx_a.chunks if c.layer is ContextLayer.L0)
    l0_b = next(c for c in ctx_b.chunks if c.layer is ContextLayer.L0)
    assert l0_a.content == l0_b.content


def test_context_budget_ratio_must_sum_to_one():
    with pytest.raises(ValueError):
        ContextBudget(l0_ratio=0.5, l1_ratio=0.5, l2_ratio=0.5, l4_ratio=0.0, reserve_ratio=0.0)


def test_build_coverage_manifest():
    file = _file_from_diff(EVAL_DIFF)
    manifest = build_coverage(file, BUILTIN_RULES, truncated_l2=True, stage=StageName.CONTEXT)
    reasons = {item.reason for item in manifest.items}
    assert CoverageReason.COVERED in reasons
    assert CoverageReason.TRUNCATED in reasons
    assert manifest.truncated is True
    rule_items = [i for i in manifest.items if i.target.startswith("python.")]
    assert len(rule_items) >= 2
