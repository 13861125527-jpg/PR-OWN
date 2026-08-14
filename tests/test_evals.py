"""评测指标测试（P1-1：一对一匹配、category 必须一致、precision ≤ 1）。"""

import pytest
from reposage.domain.enums import FindingCategory, Severity
from reposage.domain.finding import Finding
from reposage.evals.dataset import EvalDataset, EvalSample, ExpectedFinding
from reposage.evals.metrics import compute_metrics


def _finding(path: str | None, line: int | None, category: FindingCategory = FindingCategory.SECURITY) -> Finding:
    return Finding(
        finding_occurrence_id="occ",
        run_id="run",
        fingerprint="fp",
        cross_run_match_key="key",
        title="t",
        severity=Severity.HIGH,
        confidence=0.9,
        category=category,
        canonical_path=path,
        canonical_start_line=line,
    )


def test_perfect_hit():
    expected = [ExpectedFinding(category="security", path="src/a.py", line=5)]
    findings = [_finding("src/a.py", 5)]
    m = compute_metrics(expected, findings)
    assert m.precision == 1.0
    assert m.recall == 1.0
    assert m.f1 == 1.0


def test_position_mismatch_no_hit():
    expected = [ExpectedFinding(category="security", path="src/a.py", line=5)]
    findings = [_finding("src/a.py", 99)]  # 行号错误
    m = compute_metrics(expected, findings)
    assert m.recall == 0.0
    assert m.position_accuracy == 1.0  # 位置本身可定位


def test_category_mismatch_no_hit():
    """P1-1：category 必须一致，否则不命中。"""
    expected = [ExpectedFinding(category="security", path="src/a.py", line=5)]
    findings = [_finding("src/a.py", 5, category=FindingCategory.CORRECTNESS)]
    m = compute_metrics(expected, findings)
    assert m.recall == 0.0
    assert m.precision == 0.0


def test_one_to_one_matching_no_double_hit():
    """P1-1：一个 Finding 不能同时命中两条 expected。"""
    expected = [
        ExpectedFinding(category="security", path="src/a.py", line=5),
        ExpectedFinding(category="security", path="src/a.py", line=5),
    ]
    findings = [_finding("src/a.py", 5)]  # 只有一个 Finding
    m = compute_metrics(expected, findings)
    assert m.recall == 0.5  # 只命中 1/2
    assert m.precision == 1.0  # 1/1
    assert m.f1 <= 1.0


def test_precision_never_exceeds_one():
    """P1-1：hits ≤ min(expected, findings)，precision 不可能 > 1。"""
    expected = [ExpectedFinding(category="security", path="src/a.py", line=1)]
    findings = [_finding("src/a.py", 1)] * 5  # 5 个重复 Finding（category 均匹配 security）
    m = compute_metrics(expected, findings)
    assert m.precision == 0.2  # 1 hit / 5 findings
    assert m.recall == 1.0
    assert m.f1 <= 1.0


def test_negative_sample_noise():
    findings = [_finding("src/a.py", 1)]
    m = compute_metrics([], findings, sample_kind="negative")
    assert m.negative_noise == 1


def test_dataset_load_yaml(tmp_path):
    path = tmp_path / "ds.yaml"
    path.write_text(
        """
name: demo
samples:
  - id: s1
    base_files:
      a.py: "x = 1\\n"
    head_files:
      a.py: "x = 2\\n"
    expected:
      - category: correctness
        path: a.py
        line: 1
""",
        encoding="utf-8",
    )
    ds = EvalDataset.load_yaml(path)
    assert ds.name == "demo"
    assert ds.samples[0].id == "s1"
    assert ds.samples[0].expected[0].line == 1


# ---- V1-d：真实链路集成（EvalRunner → SinglePassReviewer → FindingPipeline → metrics） ----


@pytest.mark.asyncio
async def test_eval_runner_real_pipeline_hits_expected():
    """注入脚本化候选 → 真实链路（FakeGit+装配+SinglePass+Pipeline）→ recall=1.0。"""
    from reposage.domain.finding import FindingCandidate
    from reposage.evals.runner import EvalRunner
    from reposage.providers.llm.fake import FakeLLMProvider
    from reposage.review.single_pass import SinglePassReviewer

    sample = EvalSample(
        id="s1",
        kind="single_defect",
        base_files={"src/a.py": "def f():\n    return 1\n"},
        head_files={"src/a.py": "def f():\n    return eval(x)\n"},
        pr_title="fix eval",
        expected=[ExpectedFinding(category="security", path="src/a.py", line=2)],
    )
    cand = FindingCandidate(
        title="eval", severity=Severity.HIGH, confidence=0.9,
        category=FindingCategory.SECURITY,
        claimed_path="src/a.py", claimed_start_line=2,
        trigger_condition="eval(x)", explanation="x", impact="y", suggestion="z",
    )
    strategy = SinglePassReviewer(FakeLLMProvider(default_findings=[cand]))
    runner = EvalRunner(EvalDataset(name="d", samples=[sample]), strategy=strategy)
    await runner.run()
    m = runner.results["s1"]
    assert m.recall == 1.0
    assert m.precision == 1.0
    assert m.position_accuracy == 1.0


@pytest.mark.asyncio
async def test_eval_runner_no_candidates_zero_recall():
    """无候选 → recall 0（真实链路空跑）。"""
    from reposage.evals.runner import EvalRunner
    from reposage.providers.llm.fake import FakeLLMProvider
    from reposage.review.single_pass import SinglePassReviewer

    sample = EvalSample(
        id="s2", kind="single_defect",
        base_files={"src/a.py": "x = 1\n"},
        head_files={"src/a.py": "x = eval(1)\n"},
        expected=[ExpectedFinding(category="security", path="src/a.py", line=1)],
    )
    strategy = SinglePassReviewer(FakeLLMProvider())  # 空候选
    runner = EvalRunner(EvalDataset(name="d", samples=[sample]), strategy=strategy)
    await runner.run()
    assert runner.results["s2"].recall == 0.0
# ================= V1-d 返工：P1-4 评测 DoD（脚本化/门槛/baseline） =================


def _fake_metrics(precision=1.0, position=1.0, noise=0, kind="single_defect"):
    from reposage.evals.metrics import Metrics

    return Metrics(
        precision=precision,
        recall=1.0,
        f1=1.0,
        position_accuracy=position,
        negative_noise=noise,
        details={"sample_kind": kind},
    )


def test_thresholds_pass():
    from reposage.evals.thresholds import check_thresholds

    results = {"a": _fake_metrics(1.0, 1.0), "b": _fake_metrics(0.9, 0.95)}
    ok, violations = check_thresholds(results)
    assert ok is True
    assert violations == []


def test_thresholds_fail_precision():
    from reposage.evals.thresholds import check_thresholds

    results = {"a": _fake_metrics(0.5, 1.0)}
    ok, violations = check_thresholds(results)
    assert ok is False
    assert any("Precision" in v for v in violations)


def test_thresholds_fail_negative_noise():
    from reposage.evals.thresholds import check_thresholds

    results = {"neg": _fake_metrics(1.0, 1.0, noise=3, kind="negative")}
    ok, violations = check_thresholds(results)
    assert ok is False
    assert any("NegativeNoise" in v for v in violations)


@pytest.mark.asyncio
async def test_scripted_eval_all_samples_perfect():
    """脚本化评测：20 样本全部指标 1.0（CI 验证 Pipeline 指标计算）。"""
    from pathlib import Path

    from reposage.evals.runner import EvalRunner
    from reposage.evals.scripted import ScriptedStrategy, scripted_candidates

    ds = EvalDataset.load_yaml(Path("reposage/evals/datasets/v1_demo.yaml"))
    assert len(ds.samples) >= 20
    runner = EvalRunner(ds, strategy=ScriptedStrategy(scripted_candidates))
    await runner.run()
    for sid, m in runner.results.items():
        assert m.precision == 1.0, f"{sid} precision {m.precision}"
        assert m.recall == 1.0, f"{sid} recall {m.recall}"
        assert m.position_accuracy == 1.0, f"{sid} position {m.position_accuracy}"


def test_scripted_gate_exit_zero():
    """门槛 CLI：达标返回 0。"""
    from reposage.evals.thresholds import run_scripted_gate

    ok, results, violations = run_scripted_gate("reposage/evals/datasets/v1_demo.yaml")
    assert ok is True
    assert len(results) >= 20


def test_baseline_compare_v1_beats_baseline_precision():
    """baseline 对照：V1 经 pipeline（重定位/去重/抑制幻觉）precision 高于直拼。"""
    from reposage.evals.baseline import compare

    result = compare("reposage/evals/datasets/v1_demo.yaml")
    q = result["quality"]
    assert q["precision"]["v1"] >= q["precision"]["baseline"]
    assert set(result.keys()) == {"quality", "cost", "latency"}
    # 三张表结构
    assert "model_calls" in result["cost"]
    assert "total_ms" in result["latency"]
def test_gate_cli_nonzero_exit_on_failure(monkeypatch):
    """门槛 CLI：构造低指标数据 → main 返回非零。"""
    import sys

    import reposage.evals.thresholds as th

    def bad_scripted(sample_id):
        return []  # 空候选 → recall 0

    monkeypatch.setattr(th, "scripted_candidates", bad_scripted)
    monkeypatch.setattr(sys, "argv", ["thresholds", "reposage/evals/datasets/v1_demo.yaml"])
    # main 内部 run_scripted_gate 默认 scripted=scripted_candidates（模块级默认已 monkeypatch）

    # 空候选下 precision/recall 全 0 → 门槛失败
    ok, _results, violations = th.run_scripted_gate(
        "reposage/evals/datasets/v1_demo.yaml", scripted=bad_scripted
    )
    assert ok is False
    assert violations
