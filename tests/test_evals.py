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
    assert m.position_accuracy == 1.0  # 位置与 expected 完全一致


def test_position_mismatch_no_hit():
    """P1-2：行号错误 → 位置准确率 0.0（不再只看"是否有位置"）。"""
    expected = [ExpectedFinding(category="security", path="src/a.py", line=5)]
    findings = [_finding("src/a.py", 99)]  # 行号错误
    m = compute_metrics(expected, findings)
    assert m.recall == 0.0
    assert m.position_accuracy == 0.0


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


def test_position_accuracy_zero_when_no_matching_finding():
    """P1-2：有 expected 却完全没匹配到 finding → 位置准确率 0（不因分母为 0 返回 1.0）。"""
    expected = [ExpectedFinding(category="security", path="src/a.py", line=5)]
    m = compute_metrics(expected, [])  # 无 finding
    assert m.recall == 0.0
    assert m.position_accuracy == 0.0


def test_position_matching_prefers_nearest_line():
    """P1-2：多 expected 同 (category,path) 时按行号距离最近匹配，不因贪婪顺序低估。"""
    expected = [
        ExpectedFinding(category="security", path="src/a.py", line=5),
        ExpectedFinding(category="security", path="src/a.py", line=6),
    ]
    findings = [_finding("src/a.py", 6), _finding("src/a.py", 5)]  # 逆序
    m = compute_metrics(expected, findings)
    assert m.recall == 1.0
    assert m.position_accuracy == 1.0  # 最近距离匹配：5→5、6→6


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


def _fake_metrics(precision=1.0, position=1.0, noise=0, kind="single_defect", n_expected=1):
    from reposage.evals.metrics import Metrics

    return Metrics(
        precision=precision,
        recall=1.0,
        f1=1.0,
        position_accuracy=position,
        negative_noise=noise,
        details={"sample_kind": kind, "n_expected": n_expected},
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


def test_thresholds_position_excludes_no_expected_samples():
    """P1-2：无 expected 样本的 position_accuracy=1.0 不参与 macro，避免拉高有缺陷样本。"""
    from reposage.evals.thresholds import check_thresholds

    results = {
        "pos": _fake_metrics(1.0, 0.5, kind="single_defect", n_expected=1),  # 有缺陷样本位置准确率 0.5
        "neg": _fake_metrics(1.0, 1.0, kind="negative", n_expected=0),  # 无 expected 位置准确率 1.0
        "rob": _fake_metrics(1.0, 1.0, kind="robustness", n_expected=0),  # 无 expected（s12 类）也不参与
    }
    ok, violations = check_thresholds(results)
    # macro 位置准确率 = 0.5（排除无 expected 的 1.0）→ 低于 0.8 门槛 → 失败
    assert ok is False
    assert any("PositionAcc" in v for v in violations)


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
# ================= V1-d 三轮：真实模型对照入口（real_compare） =================


@pytest.mark.asyncio
async def test_real_compare_runs_both_sides_with_fake_llm():
    """P1-3：真实对照入口用注入 LLM 可跑通 V1 与 baseline 两侧并产出指标。"""
    from reposage.domain.finding import FindingCandidate
    from reposage.evals import real_compare
    from reposage.providers.llm.fake import FakeLLMProvider

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
    fake = FakeLLMProvider(default_findings=[cand], input_tokens=100, output_tokens=50)
    res = await real_compare._run_sample(
        sample, fake, repeats=2, input_price=None, output_price=None
    )
    # 两侧都产出 metrics（不要求全 1.0，只验证链路可运行且结构完整）
    assert res.v1_metrics is not None and res.base_metrics is not None
    assert res.v1_run_latency_ms >= 0.0 and res.base_run_latency_ms >= 0.0
    assert res.failures == []
    # 单文件单 unit：V1 repeats=2 → 2 次调用；baseline repeats=2 → 2 次调用
    assert res.v1_model_calls == 2
    assert res.base_model_calls == 2
    # token 按每次调用累计（100 input + 50 output per call）
    assert res.v1_input_tokens == 200 and res.v1_output_tokens == 100
    assert res.base_input_tokens == 200 and res.base_output_tokens == 100


@pytest.mark.asyncio
async def test_real_compare_multifile_units_count_calls():
    """P1-3（四轮）：多文件样本 → V1 拆多个 unit 多次调用，model_calls 按真实调用累计。"""
    from reposage.domain.finding import FindingCandidate
    from reposage.evals import real_compare
    from reposage.providers.llm.fake import FakeLLMProvider

    sample = EvalSample(
        id="s3",
        kind="multi_defect",
        base_files={
            "src/a.py": "def fa():\n    return 1\n",
            "src/b.py": "def fb():\n    return 2\n",
        },
        head_files={
            "src/a.py": "def fa():\n    return eval(x)\n",
            "src/b.py": "def fb():\n    return eval(y)\n",
        },
        expected=[
            ExpectedFinding(category="security", path="src/a.py", line=2),
            ExpectedFinding(category="security", path="src/b.py", line=2),
        ],
    )
    cand = FindingCandidate(
        title="eval", severity=Severity.HIGH, confidence=0.9,
        category=FindingCategory.SECURITY,
        claimed_path="src/a.py", claimed_start_line=2,
        trigger_condition="eval(x)", explanation="x", impact="y", suggestion="z",
    )
    fake = FakeLLMProvider(default_findings=[cand], input_tokens=100, output_tokens=50)
    res = await real_compare._run_sample(
        sample, fake, repeats=1, input_price=None, output_price=None
    )
    # 两个文件各一 unit → V1 2 次真实调用；baseline 直拼只 1 次
    assert res.v1_model_calls == 2
    assert res.base_model_calls == 1
    assert res.v1_input_tokens == 200
    assert res.base_input_tokens == 100
    assert res.failures == []


@pytest.mark.asyncio
async def test_real_compare_records_v1_task_failures():
    """P1（四轮 should-fix）：V1 侧 LLM 异常被转 FAILED task → 记入 failures。"""

    class _BoomLLM:
        async def structured(self, messages, **kwargs):
            raise RuntimeError("llm boom")

        async def complete(self, *a, **k):
            raise NotImplementedError

        async def tool_loop(self, *a, **k):
            raise NotImplementedError

    from reposage.evals import real_compare

    sample = EvalSample(
        id="s4",
        kind="single_defect",
        base_files={"src/a.py": "def fa():\n    return 1\n"},
        head_files={"src/a.py": "def fa():\n    return eval(x)\n"},
        expected=[ExpectedFinding(category="security", path="src/a.py", line=2)],
    )
    res = await real_compare._run_sample(
        sample, _BoomLLM(), repeats=1, input_price=None, output_price=None
    )
    # V1 侧 LLM 异常被 SinglePassReviewer 转成 FAILED task（不向上抛）→ 必须记入 failures
    assert any(f["side"] == "v1" and f["kind"] == "TaskFailed" for f in res.failures)
    assert res.v1_metrics is not None and res.v1_metrics.recall == 0.0
    report = real_compare._build_report(
        [res],
        model="m",
        dataset="ds.yaml",
        repeats=1,
        temperature=0.1,
        max_output_tokens=3000,
        base_url="https://api.example.com/v1",
    )
    assert report["execution_completed"] is False  # 任务失败 → 执行不完整
    assert report["failures"]  # 失败已记录


@pytest.mark.asyncio
async def test_real_compare_records_failures_per_side():
    """P1-3：某侧 LLM 失败 → 记录失败样本，不中断另一侧。"""
    from reposage.evals import real_compare

    class _BoomLLM:
        async def structured(self, messages, **kwargs):
            raise RuntimeError("llm boom")

        async def complete(self, *a, **k):
            raise NotImplementedError

        async def tool_loop(self, *a, **k):
            raise NotImplementedError

    sample = EvalSample(
        id="s2",
        kind="single_defect",
        base_files={"src/a.py": "x = 1\n"},
        head_files={"src/a.py": "x = eval(1)\n"},
        expected=[ExpectedFinding(category="security", path="src/a.py", line=1)],
    )
    res = await real_compare._run_sample(
        sample, _BoomLLM(), repeats=1, input_price=None, output_price=None
    )
    # V1 侧：LLM 异常被 SinglePassReviewer 转成文件任务 FAILED → 0 指标（不抛、不中断）
    assert res.v1_metrics is not None and res.v1_metrics.recall == 0.0
    # baseline 侧：直拼调用直接抛异常 → 无指标 + 失败记录
    assert res.base_metrics is None
    assert res.failures, "应有失败记录"
    assert any("boom" in f["detail"] for f in res.failures)


def test_real_compare_report_structure_and_redaction():
    """P1-3：报告含 summary/cost/latency/per_sample，且 base_url 脱敏。"""
    from reposage.evals import real_compare
    from reposage.evals.metrics import Metrics

    ok = Metrics(
        precision=1.0, recall=1.0, f1=1.0, position_accuracy=1.0,
        details={"sample_kind": "single_defect", "n_expected": 1},
    )
    res = real_compare.SampleResult(
        sample_id="s1",
        v1_metrics=ok,
        base_metrics=ok,
        v1_model_calls=2,
        v1_input_tokens=200,
        v1_output_tokens=100,
        base_model_calls=1,
        base_input_tokens=100,
        base_output_tokens=50,
        v1_cost_total=0.01,
        base_cost_total=0.02,
        v1_successful_runs=1,
        base_successful_runs=1,
        v1_pricing_status="known",
        base_pricing_status="known",
    )
    report = real_compare._build_report(
        [res],
        model="m",
        dataset="ds.yaml",
        repeats=1,
        temperature=0.1,
        max_output_tokens=3000,
        base_url="https://user:secret@api.example.com/v1?x=1",
    )
    assert set(report.keys()) >= {"summary", "cost", "latency", "per_sample", "failures"}
    assert report["base_url"] == "https://api.example.com/v1"  # 凭证与查询参数已剥离
    assert report["execution_completed"] is True
    assert report["quality_gate_passed"] is True  # 全 1.0 达标
    assert report["comparison_result"] == "equal"  # 两侧 F1 相同
    assert report["cost"]["v1"]["total_cost_usd"] == 0.01
    assert report["cost"]["v1"]["avg_cost_per_invocation_usd"] == 0.005  # 0.01 / 2 次逻辑调用
    assert report["cost"]["v1"]["pricing_status"] == "known"  # 已定价不误报 unknown
    assert report["cost"]["baseline"]["pricing_status"] == "known"
    assert report["cost"]["v1"]["successful_logical_invocations"] == 2  # 按真实调用累计，非样本数×repeats
    assert report["cost"]["v1"]["input_tokens"] == 200
    assert report["cost"]["baseline"]["successful_logical_invocations"] == 1
    assert report["cost"]["baseline"]["output_tokens"] == 50
    assert "failures" in report


def test_real_compare_pricing_known_not_swallowed_by_default():
    """P1（五轮/六轮）：已定价运行不应被默认 unknown 吞掉——uninitialized → known。"""
    from reposage.evals import real_compare

    res = real_compare.SampleResult(sample_id="s1")  # 默认 uninitialized
    assert res.v1_pricing_status == "uninitialized"
    assert real_compare._merge_pricing(res.v1_pricing_status, "known") == "known"
    assert real_compare._merge_pricing(res.v1_pricing_status, "unknown") == "unknown"
    # 跨样本聚合：known 样本不返回 unknown 兜底
    res.v1_pricing_status = "unknown"
    assert real_compare._pricing_agg([res], "v1_pricing_status") == "unknown"
    res2 = real_compare.SampleResult(sample_id="s2")
    res2.v1_pricing_status = "known"
    assert real_compare._pricing_agg([res2], "v1_pricing_status") == "known"


def test_real_compare_v1_and_baseline_pricing_independent():
    """P1（六轮）：V1=overrun、baseline=known 两侧状态独立，不互相污染。"""
    from reposage.evals import real_compare

    res = real_compare.SampleResult(
        sample_id="s1",
        v1_pricing_status="overrun",
        base_pricing_status="known",
    )
    assert real_compare._pricing_agg([res], "v1_pricing_status") == "overrun"
    assert real_compare._pricing_agg([res], "base_pricing_status") == "known"
    report = real_compare._build_report(
        [res],
        model="m",
        dataset="ds.yaml",
        repeats=1,
        temperature=0.1,
        max_output_tokens=3000,
        base_url="https://api.example.com/v1",
    )
    assert report["cost"]["v1"]["pricing_status"] == "overrun"
    assert report["cost"]["baseline"]["pricing_status"] == "known"


def test_real_compare_cost_total_vs_avg():
    """P1（五轮/十四轮）：cost 总量与每次逻辑调用平均分开。"""
    from reposage.evals import real_compare
    from reposage.evals.metrics import Metrics

    ok = Metrics(
        precision=1.0, recall=1.0, f1=1.0, position_accuracy=1.0,
        details={"sample_kind": "single_defect", "n_expected": 1},
    )
    # v1：6 次逻辑调用共 0.3 美元 → 每次 0.05；baseline：3 次共 0.6 → 每次 0.2
    res = real_compare.SampleResult(
        sample_id="s1",
        v1_metrics=ok,
        base_metrics=ok,
        v1_cost_total=0.3,
        base_cost_total=0.6,
        v1_successful_runs=3,
        base_successful_runs=3,
        v1_model_calls=6,
        base_model_calls=3,
        v1_input_tokens=600,
        base_input_tokens=300,
        v1_pricing_status="known",
        base_pricing_status="known",
    )
    report = real_compare._build_report(
        [res],
        model="m",
        dataset="ds.yaml",
        repeats=3,
        temperature=0.1,
        max_output_tokens=3000,
        base_url="https://api.example.com/v1",
    )
    c = report["cost"]
    assert c["v1"]["total_cost_usd"] == 0.3  # 总量，不是平均
    assert c["v1"]["avg_cost_per_invocation_usd"] == 0.05  # 0.3 / 6 次逻辑调用
    assert c["v1"]["successful_logical_invocations"] == 6  # 同总量口径
    assert c["v1"]["input_tokens"] == 600
    assert c["baseline"]["total_cost_usd"] == 0.6
    assert c["baseline"]["avg_cost_per_invocation_usd"] == 0.2  # 0.6 / 3


def test_real_compare_pricing_agg_uninitialized_fallback():
    """P1（五轮）：全 uninitialized → unknown 兜底；successful_runs=0 不除零。"""
    from reposage.evals import real_compare

    res = real_compare.SampleResult(sample_id="s1", v1_cost_total=0.0, v1_successful_runs=0)
    assert real_compare._pricing_agg([res], "v1_pricing_status") == "unknown"  # 无成功调用 → 兜底 unknown
    report = real_compare._build_report(
        [res],
        model="m",
        dataset="ds.yaml",
        repeats=1,
        temperature=0.1,
        max_output_tokens=3000,
        base_url="https://api.example.com/v1",
    )
    # successful_runs=0 时 avg 分母被 max(1, ...) 保护，不抛除零
    assert report["cost"]["v1"]["avg_cost_per_invocation_usd"] == 0.0
    assert report["cost"]["baseline"]["avg_cost_per_invocation_usd"] == 0.0


@pytest.mark.asyncio
async def test_real_compare_diagnostics_trace_redacted_and_complete():
    """P0（八轮）：诊断轨迹含候选/finding(含 suppressed reason)/expected 匹配，且不含源码。"""
    from reposage.domain.finding import FindingCandidate
    from reposage.evals import real_compare
    from reposage.providers.llm.fake import FakeLLMProvider

    sample = EvalSample(
        id="s-diag",
        kind="single_defect",
        base_files={"src/a.py": "def f():\n    return 1\n"},
        head_files={"src/a.py": "def f():\n    return eval(x)\n"},
        expected=[ExpectedFinding(category="security", path="src/a.py", line=2)],
    )
    cand = FindingCandidate(
        title="eval", severity=Severity.HIGH, confidence=0.9,
        category=FindingCategory.SECURITY,
        claimed_path="src/a.py", claimed_start_line=2,
        trigger_condition="eval(x)", explanation="x", impact="y", suggestion="z",
    )
    fake = FakeLLMProvider(default_findings=[cand], input_tokens=100, output_tokens=50)
    res = await real_compare._run_sample(
        sample, fake, repeats=1, input_price=None, output_price=None
    )
    d = res.diagnostics
    assert d["expected"] == [{"category": "security", "path": "src/a.py", "line": 2}]
    # V1 候选与 finding 轨迹完整
    assert len(d["v1_candidates"]) == 1
    assert d["v1_candidates"][0]["category"] == "security"
    assert d["v1_candidates"][0]["claimed_start_line"] == 2
    assert len(d["v1_findings"]) == 1
    f = d["v1_findings"][0]
    assert f["status"] == "accepted"
    assert f["canonical_path"] == "src/a.py" and f["canonical_start_line"] == 2
    # expected 命中
    assert d["v1_matched_expected_ids"] == [0]
    assert d["base_matched_expected_ids"] == [0]
    # 脱敏：diagnostics 序列化后不含源码内容
    import json

    blob = json.dumps(d, ensure_ascii=False)
    assert "eval(x)" not in blob
    assert "return 1" not in blob


def test_real_compare_redacts_claimed_path_secrets():
    """P0（八轮安全审查）：claimed_path 为模型自由文本，需凭证清洗。"""
    from reposage.domain.finding import FindingCandidate
    from reposage.evals import real_compare

    cand = FindingCandidate(
        title="x", severity=Severity.HIGH, confidence=0.9,
        category=FindingCategory.SECURITY,
        claimed_path="src/a.py sk-abcdef1234567890 Bearer xyz", claimed_start_line=1,
    )
    t = real_compare._candidate_trace(cand)
    assert "sk-abcdef1234567890" not in t["claimed_path"]
    assert "<redacted>" in t["claimed_path"]


@pytest.mark.asyncio
async def test_real_compare_diagnostics_capture_suppressed_reason():
    """P0（八轮）：被抑制候选在 diagnostics 中保留 suppressed 状态与 reason。"""
    from reposage.domain.finding import FindingCandidate
    from reposage.evals import real_compare
    from reposage.providers.llm.fake import FakeLLMProvider

    sample = EvalSample(
        id="s-supp",
        kind="single_defect",
        base_files={"src/a.py": "def f():\n    return 1\n"},
        head_files={"src/a.py": "def f():\n    return eval(x)\n"},
        expected=[ExpectedFinding(category="security", path="src/a.py", line=2)],
    )
    # 幻觉路径候选：canonical 无法定位 → suppressed（UNKNOWN_PATH）
    cand = FindingCandidate(
        title="hallucinated", severity=Severity.HIGH, confidence=0.9,
        category=FindingCategory.SECURITY,
        claimed_path="src/nonexistent.py", claimed_start_line=1,
    )
    fake = FakeLLMProvider(default_findings=[cand], input_tokens=100, output_tokens=50)
    res = await real_compare._run_sample(
        sample, fake, repeats=1, input_price=None, output_price=None
    )
    d = res.diagnostics
    assert len(d["v1_findings"]) == 1
    f = d["v1_findings"][0]
    assert f["status"] == "suppressed"
    assert f["reason"]  # 抑制 reason 被记录（幻觉路径等）


@pytest.mark.asyncio
async def test_real_compare_failed_call_usage_accounted():
    """P1（十二轮）：StructuredOutputError 携带 usage，失败调用计入 attempted/failed 成本。"""
    from reposage.domain.models import ModelUsage
    from reposage.evals import real_compare

    # baseline 侧失败：StructuredOutputError 带 usage → failed 计数与 token 被累计
    class _FailLLM:
        async def structured(self, messages, **kwargs):
            raise real_compare.StructuredOutputError(
                "解析失败", detail="boom",
                usage=ModelUsage(model="m", role="s", input_tokens=100, output_tokens=20,
                                 retries=0, schema_repairs=1, latency_ms=500),
            )

        async def complete(self, *a, **k):
            raise NotImplementedError

        async def tool_loop(self, *a, **k):
            raise NotImplementedError

    sample = EvalSample(
        id="s-fail",
        kind="single_defect",
        base_files={"src/a.py": "x = 1\n"},
        head_files={"src/a.py": "x = eval(1)\n"},
        expected=[ExpectedFinding(category="security", path="src/a.py", line=1)],
    )
    res = await real_compare._run_sample(
        sample, _FailLLM(), repeats=1, input_price=10.0, output_price=20.0  # 配置价格
    )
    # baseline 失败调用被计入（V1 侧走 SinglePassReviewer，异常被转 FAILED task 不抛）
    assert res.base_failed_calls == 1
    assert res.base_failed_input_tokens == 100
    assert res.base_failed_schema_repairs == 1
    # 失败成本按 token 定价：100/1000*10 + 20/1000*20 = 1.0 + 0.4 = 1.4
    assert res.base_failed_cost == pytest.approx(1.4)
    # 失败延迟计入 provider 级
    assert res.base_provider_latency_ms == 500
    report = real_compare._build_report(
        [res],
        model="m", dataset="ds.yaml", repeats=1, temperature=0.1,
        max_output_tokens=3000, base_url="https://api.example.com/v1",
    )
    c = report["cost"]["baseline"]
    assert c["logical_invocations"] == 1
    assert c["failed_logical_invocations"] == 1
    assert c["successful_logical_invocations"] == 0
    # 一次逻辑调用 = 1 基础 + 1 schema_repair = 2 次 provider 请求
    assert c["provider_requests"] == 2
    assert c["input_tokens"] == 100
    assert c["schema_repairs"] == 1
    assert c["total_cost_usd"] == pytest.approx(1.4)  # 失败成本进入总量
    # 失败延迟进入 provider 级汇总
    lat = report["latency"]["baseline"]
    assert lat["provider_latency"]["total_ms"] == 500
    assert lat["provider_latency"]["avg_ms"] == 500  # 500ms / 1 逻辑调用


def test_real_compare_paired_success_comparison():
    """P1（十二轮）：comparison_result 基于 paired-success 集合，非不同样本集合。"""
    from reposage.evals import real_compare
    from reposage.evals.metrics import Metrics

    def m(f1):
        return Metrics(precision=1.0, recall=1.0, f1=f1, position_accuracy=1.0,
                       details={"sample_kind": "single_defect", "n_expected": 1})

    # 三个样本：s1 两侧都成功（V1 F1 高），s2 只有 V1 成功（V1 F1 极高），s3 只有 baseline 成功（baseline F1 高）
    r1 = real_compare.SampleResult(sample_id="s1", v1_metrics=m(0.8), base_metrics=m(0.6))
    r2 = real_compare.SampleResult(sample_id="s2", v1_metrics=m(1.0), base_metrics=None)  # V1-only
    r3 = real_compare.SampleResult(sample_id="s3", v1_metrics=None, base_metrics=m(1.0))  # baseline-only
    report = real_compare._build_report(
        [r1, r2, r3], model="m", dataset="ds.yaml", repeats=1, temperature=0.1,
        max_output_tokens=3000, base_url="https://api.example.com/v1",
    )
    # paired-success 只含 s1
    assert report["paired_summary"]["sample_ids"] == ["s1"]
    assert report["paired_summary"]["sample_count"] == 1
    # comparison 基于 paired：V1 F1 0.8 > baseline 0.6 → improved
    assert report["comparison_result"] == "improved"
    # missing 列出非配对样本
    assert report["missing"]["v1"] == ["s3"]
    assert report["missing"]["baseline"] == ["s2"]


def test_real_compare_v1_failed_cost_not_double_counted():
    """P1（十三轮审查 blocking）：V1 失败成本通过 budget.cost_used 计入一次，total 不双计。"""
    from reposage.evals import real_compare

    # V1 侧失败 usage 已在 budget 结算（v1_cost_total 含失败成本），v1_failed_cost 应为 0
    # （失败成本不单独再累加，避免与 budget.cost_used 双计）
    res = real_compare.SampleResult(
        sample_id="s1",
        v1_cost_total=1.4,  # 含成功 + 失败成本（budget 结算）
        v1_failed_cost=0.0,  # 不再单独累加
        v1_failed_calls=1,
        v1_failed_input_tokens=100,
        v1_failed_output_tokens=20,
        v1_provider_latency_ms=500,
        v1_successful_runs=1,
        v1_provider_requests=2,
        base_cost_total=0.0,
        base_failed_cost=0.0,
        v1_pricing_status="known",
        base_pricing_status="known",
    )
    report = real_compare._build_report(
        [res], model="m", dataset="ds.yaml", repeats=1, temperature=0.1,
        max_output_tokens=3000, base_url="https://api.example.com/v1",
    )
    # total = v1_cost_total（已含失败）+ v1_failed_cost（仅顶层异常）= 1.4，不双计为 2.8
    assert report["cost"]["v1"]["total_cost_usd"] == pytest.approx(1.4)
    assert report["cost"]["v1"]["failed_logical_invocations"] == 1
    assert report["cost"]["v1"]["provider_requests"] == 2
    assert report["latency"]["v1"]["provider_latency"]["total_ms"] == 500


def test_real_compare_run_latency_weighted_average():
    """P1（十四轮）：repeats=2 两次成功各 100ms → run 成功平均仍 100ms（不除以 2）；加权平均。"""
    from reposage.evals import real_compare

    # s1：repeats=2，每次成功 100ms → run 总量 200ms、成功次数 2 → 平均 100ms
    r1 = real_compare.SampleResult(
        sample_id="s1", v1_run_latency_ms=200.0, v1_successful_runs=2,
        base_run_latency_ms=200.0, base_successful_runs=2,
        v1_pricing_status="known", base_pricing_status="known",
    )
    # s2：repeats=1，成功 300ms
    r2 = real_compare.SampleResult(
        sample_id="s2", v1_run_latency_ms=300.0, v1_successful_runs=1,
        base_run_latency_ms=300.0, base_successful_runs=1,
        v1_pricing_status="known", base_pricing_status="known",
    )
    report = real_compare._build_report(
        [r1, r2], model="m", dataset="ds.yaml", repeats=2, temperature=0.1,
        max_output_tokens=3000, base_url="https://api.example.com/v1",
    )
    rl = report["latency"]["v1"]["run_latency"]
    # 加权平均 = (200 + 300) / (2 + 1) = 166.67，不是 (100+300)/2=200 的样本平均平均
    assert rl["success_total_ms"] == 500
    assert rl["success_count"] == 3
    assert rl["success_avg_ms"] == pytest.approx(166.67, abs=0.01)


def test_real_compare_failed_run_latency_not_double_counted():
    """P1（十四轮）：一次 review 内含成功+失败调用，run 级端到端延迟不含失败 usage 耗时。"""
    from reposage.evals import real_compare

    # run 级端到端耗时 100ms（已含内部失败等待）；provider 级 usage 耗时 500ms（单独口径）
    res = real_compare.SampleResult(
        sample_id="s1",
        v1_run_latency_ms=100.0,  # 端到端（已含失败等待，不重复加 usage 耗时）
        v1_successful_runs=1,
        v1_provider_latency_ms=500.0,  # provider 级独立累计
        v1_provider_requests=2,
        v1_failed_calls=1,
        v1_pricing_status="known",
        base_pricing_status="known",
    )
    report = real_compare._build_report(
        [res], model="m", dataset="ds.yaml", repeats=1, temperature=0.1,
        max_output_tokens=3000, base_url="https://api.example.com/v1",
    )
    lat = report["latency"]["v1"]
    # run 级成功平均 = 100ms（不含失败 usage 的 500ms）
    assert lat["run_latency"]["success_avg_ms"] == 100.0
    # provider 级独立 = 500ms / 1 逻辑调用 = 500ms（usage.latency_ms 是每次逻辑调用耗时）
    assert lat["provider_latency"]["total_ms"] == 500.0
    assert lat["provider_latency"]["avg_ms"] == 500.0


@pytest.mark.asyncio
async def test_real_compare_all_failed_pricing_status():
    """P2（十四轮）：全失败时 pricing 状态同步（已定价 known / 未定价 unknown），不再 uninitialized。"""
    from reposage.domain.models import ModelUsage
    from reposage.evals import real_compare

    class _FailLLM:
        async def structured(self, messages, **kwargs):
            raise real_compare.StructuredOutputError(
                "解析失败", detail="boom",
                usage=ModelUsage(model="m", role="s", input_tokens=100, output_tokens=20),
            )

        async def complete(self, *a, **k):
            raise NotImplementedError

        async def tool_loop(self, *a, **k):
            raise NotImplementedError

    sample = EvalSample(
        id="s-fail", kind="single_defect",
        base_files={"src/a.py": "x = 1\n"}, head_files={"src/a.py": "x = eval(1)\n"},
        expected=[ExpectedFinding(category="security", path="src/a.py", line=1)],
    )
    # 已定价：全失败 → base_pricing_status = known
    res_priced = await real_compare._run_sample(sample, _FailLLM(), repeats=1, input_price=10.0, output_price=20.0)
    assert res_priced.base_pricing_status == "known"
    # 未定价：全失败 → unknown
    res_unpriced = await real_compare._run_sample(sample, _FailLLM(), repeats=1, input_price=None, output_price=None)
    assert res_unpriced.base_pricing_status == "unknown"


@pytest.mark.asyncio
async def test_real_compare_all_failed_pricing_no_usage():
    """P2（十四轮审查 should-fix）：LLMRequestError（无 usage）全失败也同步 pricing。"""
    from reposage.evals import real_compare

    class _BoomNoUsage:
        async def structured(self, messages, **kwargs):
            raise real_compare.LLMRequestError("HTTP 500")

        async def complete(self, *a, **k):
            raise NotImplementedError

        async def tool_loop(self, *a, **k):
            raise NotImplementedError

    sample = EvalSample(
        id="s-fail", kind="single_defect",
        base_files={"src/a.py": "x = 1\n"}, head_files={"src/a.py": "x = eval(1)\n"},
        expected=[ExpectedFinding(category="security", path="src/a.py", line=1)],
    )
    res = await real_compare._run_sample(sample, _BoomNoUsage(), repeats=1, input_price=10.0, output_price=20.0)
    # 无 usage 失败：pricing 仍据价格配置同步为 known，不再 uninitialized
    assert res.base_pricing_status == "known"


def test_real_compare_resolve_output_paths_debug_isolated():
    """P2（十三轮）：--sample-ids 默认用 debug 路径，不覆盖正式证据；显式路径优先。"""
    from reposage.evals import real_compare

    # 单样本调试：默认自动 debug 路径
    out, md = real_compare._resolve_output_paths(
        sample_ids="s08", output=None, markdown=None, matched_ids=["s08-multi-defect"]
    )
    assert out == "docs/evidence/v1-d-debug-s08-multi-defect.json"
    assert md == "docs/evidence/v1-d-debug-s08-multi-defect.md"
    # 完整运行：正式路径
    out, md = real_compare._resolve_output_paths(
        sample_ids="", output=None, markdown=None, matched_ids=[]
    )
    assert out == "docs/evidence/v1-d-real-compare.json"
    assert md == "docs/evidence/v1-d-real-compare.md"
    # 显式传入 output 时始终优先（即使 sample_ids 指定）
    out, md = real_compare._resolve_output_paths(
        sample_ids="s08", output="custom.json", markdown=None, matched_ids=["s08"]
    )
    assert out == "custom.json"
    assert md == "docs/evidence/v1-d-debug-s08.md"


def test_real_compare_redacts_secrets_in_failure_detail():
    """P1-3：失败 detail 脱敏——API key / Bearer / Authorization 不落入报告。"""
    from reposage.evals import real_compare

    dirty = "Bearer sk-abcdef1234567890 sent via Authorization: sk-xyz; HTTP 400 boom"
    redacted = real_compare._redact_detail(dirty)
    assert "sk-abcdef1234567890" not in redacted
    assert "sk-xyz" not in redacted
    assert "<redacted>" in redacted
    assert "boom" in redacted  # 安全摘要保留


@pytest.mark.asyncio
async def test_v2a_compare_emits_three_tables(tmp_path):
    """V2-A T10：同一评测集产出质量/成本/延迟三表，并写出 JSON+Markdown。"""
    from reposage.evals.v2a_compare import render_markdown, run_compare, write_report

    report = await run_compare("reposage/evals/datasets/v1_demo.yaml", repeats=1)
    assert set(report) >= {"quality", "cost", "latency", "gate", "real_api"}
    for table in ("quality", "cost", "latency"):
        assert "v1" in str(report[table])
        assert "v2a" in str(report[table])
    q = report["quality"]
    for key in ("precision", "recall", "f1", "position_accuracy", "negative_noise"):
        assert key in q
        assert "v1" in q[key] and "v2a" in q[key]
    c = report["cost"]
    for key in ("model_calls", "input_tokens", "output_tokens", "total_tokens", "budget_rejects"):
        assert key in c
    lat = report["latency"]
    for key in ("total_ms", "p50_ms", "p95_ms"):
        assert key in lat
    assert report["real_api"] == "not_run"
    assert report["cost"]["model_calls"]["v2a"] >= report["cost"]["model_calls"]["v1"]
    json_path = tmp_path / "v2-a-compare.json"
    md_path = tmp_path / "v2-a-compare.md"
    write_report(report, json_path=json_path, markdown_path=md_path)
    text = md_path.read_text(encoding="utf-8")
    assert "## 质量（Finding，macro）" in text
    assert "## 成本（Fake 记账）" in text
    assert "## 延迟（全数据集墙钟）" in text
    assert "不代表多角色模型效果相同" in text
    assert render_markdown(report) == text


def test_budget_rejects_counts_once_when_task_and_truncated():
    """同一次预算拒绝同时有 failed task 与 TRUNCATED coverage 时只计 1。"""
    from reposage.domain.enums import (
        CoverageReason,
        ReviewStrategyName,
        ReviewTaskKind,
        ReviewTaskStatus,
    )
    from reposage.domain.models import CoverageItem
    from reposage.domain.run import ReviewTask, SourceRunResult
    from reposage.domain.strategy import StrategyResult
    from reposage.evals.v2a_compare import _budget_rejects

    result = StrategyResult(
        candidates=[],
        source_run=SourceRunResult(
            strategy=ReviewStrategyName.MULTI_ROLE,
            tasks=[
                ReviewTask(
                    task_id="t1",
                    run_id="r1",
                    kind=ReviewTaskKind.ROLE_REVIEW,
                    target="src/a.py",
                    status=ReviewTaskStatus.FAILED,
                    error="预算不足 BudgetExceeded",
                )
            ],
            coverage_items=[
                CoverageItem(target="src/a.py", reason=CoverageReason.TRUNCATED),
            ],
        ),
    )
    assert _budget_rejects(result) == 1


def test_l3_dataset_meets_hit_rate():
    from reposage.evals.l3 import FALSE_RATE_MAX, HIT_RATE_MIN, evaluate_l3_dataset, load_l3_dataset

    samples = load_l3_dataset()
    assert len(samples) >= 12
    metrics = evaluate_l3_dataset(samples)
    assert metrics.hit_rate >= HIT_RATE_MIN, metrics.misses
    assert metrics.false_rate <= FALSE_RATE_MAX, metrics.misses
    assert metrics.expected_total >= 6
    assert metrics.tp + metrics.fn == metrics.expected_total


@pytest.mark.asyncio
async def test_v2b_compare_emits_tables(tmp_path):
    from reposage.evals.v2b_compare import render_markdown, run_compare, write_report

    report = await run_compare("reposage/evals/datasets/v2b_compare.yaml", repeats=1)
    assert "quality" in report and "cost" in report and "latency" in report
    assert "retrieval" in report
    assert report["real_api"] == "not_run"
    assert report["cost"]["l3_chunks"]["off"] == 0
    assert report["cost"]["l3_chunks"]["on"] > 0
    assert report["cost"]["input_tokens"]["on"] > report["cost"]["input_tokens"]["off"]
    assert report["quality"]["position_accuracy"]["off"] > 0
    md_path = tmp_path / "v2-b-compare.md"
    write_report(report, json_path=tmp_path / "v2-b-compare.json", markdown_path=md_path)
    text = md_path.read_text(encoding="utf-8")
    assert "## 检索命中" in text
    assert "不代表真实模型增益" in text or "不代表模型因 L3 变强" in text
    assert render_markdown(report) == text


def test_v2c_compare_conversion_and_dedup(tmp_path):
    from reposage.evals.v2c_compare import render_markdown, run_compare, write_report

    report = run_compare("reposage/evals/datasets/v2c_static.yaml")
    assert report["all_passed"] is True
    assert report["conversion"]["passed"] == report["conversion"]["total"]
    assert report["dedup"]["passed"] == report["dedup"]["total"]
    md_path = tmp_path / "v2-c-compare.md"
    write_report(report, json_path=tmp_path / "v2-c-compare.json", markdown_path=md_path)
    text = md_path.read_text(encoding="utf-8")
    assert "## 转换" in text
    assert "## 去重 / 融合" in text
    assert render_markdown(report) == text


def test_v2d_compare_dedup_and_judge(tmp_path):
    from reposage.evals.v2d_compare import render_markdown, run_compare, write_report

    report = run_compare("reposage/evals/datasets/v2d_dedup.yaml")
    assert report["all_passed"] is True
    assert report["dedup"]["passed"] == report["dedup"]["total"]
    assert report["judge"]["passed"] == report["judge"]["total"]
    md_path = tmp_path / "v2-d-compare.md"
    write_report(report, json_path=tmp_path / "v2-d-compare.json", markdown_path=md_path)
    text = md_path.read_text(encoding="utf-8")
    assert "## 确定性去重" in text
    assert "survival" in text
    assert render_markdown(report) == text


def test_v2e_compare_feedback_suppress_and_restore(tmp_path):
    from reposage.evals.v2e_compare import render_markdown, run_compare, write_report

    report = run_compare("reposage/evals/datasets/v2e_feedback.yaml")
    assert report["all_passed"] is True
    assert report["feedback"]["passed"] == report["feedback"]["total"]
    assert report["feedback"]["global_reject"] is True
    md_path = tmp_path / "v2-e-compare.md"
    write_report(report, json_path=tmp_path / "v2-e-compare.json", markdown_path=md_path)
    text = md_path.read_text(encoding="utf-8")
    assert "## 反馈抑制" in text
    assert render_markdown(report) == text


def test_v3a_compare_sandbox_tables(tmp_path):
    from reposage.evals.v3a_compare import render_markdown, run_compare, write_report

    report = run_compare("reposage/evals/datasets/v3a_sandbox.yaml")
    assert report["all_passed"] is True
    assert report["sandbox"]["passed"] == report["sandbox"]["total"]
    md_path = tmp_path / "v3-a-compare.md"
    write_report(report, json_path=tmp_path / "v3-a-compare.json", markdown_path=md_path)
    text = md_path.read_text(encoding="utf-8")
    assert "## 越界" in text
    assert render_markdown(report) == text


def test_v3b_compare_loop_tables(tmp_path):
    from reposage.evals.v3b_compare import render_markdown, run_compare

    report = run_compare("reposage/evals/datasets/v3b_loop.yaml")
    assert report["all_passed"] is True
    assert report["loop"]["passed"] == report["loop"]["total"]
    ids = {row["id"] for row in report["loop"]["cases"]}
    assert "early-grace-cap" in ids
    assert "json-repair-counts-round" in ids
    assert "unique-same-name-tool-ids" in ids
    assert "settle-idempotent" in ids
    assert "waiting-tool-cancel" in ids
    assert "shared-pool-concurrent" in ids
    md_path = tmp_path / "v3-b-compare.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    text = md_path.read_text(encoding="utf-8")
    assert "## 轨迹 / 控制工具 / 隔离 / 预算" in text
    assert render_markdown(report) == text


def test_v3c_compare_compact_tables(tmp_path):
    from reposage.evals.v3c_compare import render_markdown, run_compare

    report = run_compare("reposage/evals/datasets/v3c_compact.yaml")
    assert report["all_passed"] is True
    assert report["compact"]["passed"] == report["compact"]["total"]
    ids = {row["id"] for row in report["compact"]["cases"]}
    assert "compact-keeps-min-l3" in ids
    assert "compact-keeps-evidence-ids" in ids
    assert "compact-not-a-round" in ids
    assert "compact-native-pairing" in ids
    assert "compact-recompact-keeps-old-facts" in ids
    assert "compact-json-repair-keeps-repair-instruction" in ids
    assert "overflow-after-compact" in ids
    assert "default-agent-off" in ids
    md_path = tmp_path / "v3-c-compare.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    text = md_path.read_text(encoding="utf-8")
    assert "## 压缩 / 证据索引 / 隔离" in text
    assert render_markdown(report) == text


def test_v3d_compare_quality_cost_latency_tables(tmp_path):
    from reposage.config.settings import Settings
    from reposage.evals import v3d_compare as v3d_mod
    from reposage.evals.v3d_compare import _v2_settings, render_markdown, run_compare, write_report
    from reposage.storage.sqlite import SqliteStorage

    report = run_compare("reposage/evals/datasets/v3d_cross_file.yaml")
    assert report["real_api"] is False
    assert report["dataset_status"] == "ok"
    assert report["dataset_errors"] == []
    assert not hasattr(v3d_mod, "_ANCHORS")
    assert not hasattr(v3d_mod, "_QUALITY_EXCLUDE")
    assert report["recommendation"] == "keep_agent_experimental"
    assert {"quality", "cost", "latency"} <= set(report)
    q = report["quality"]
    for key in ("precision", "recall", "f1", "position_accuracy", "negative_noise"):
        assert "v2" in q[key] and "v3" in q[key]
    assert "v2" in report["cost"]["model_calls"] and "v3" in report["cost"]["model_calls"]
    assert "v2" in report["latency"]["total_ms"] and "v3" in report["latency"]["total_ms"]
    assert report["cost"]["tool_calls"]["v2"] == 0
    assert "v2" in report["cost"]["cost_usd"] and "v3" in report["cost"]["cost_usd"]
    assert report["latency"]["repeats"] == 3
    assert report["agent_ops"]["v2_tool_calls"] == 0
    assert "tool_groundedness" in report["agent_ops"]
    assert "needs_evidence" in report["agent_ops"]
    # 脚本构造差值，不把 V3 F1 > V2 当产品真理（26 §9.2）
    assert "v2" in q["f1"] and "v3" in q["f1"]
    assert "不代表真实模型质量" in q["note"]
    assert report["protocol_ab"]["all_passed"] is True
    assert report["protocol_ab"]["hits_preserved"] is True
    assert report["protocol_ab"]["groundedness_preserved"] is True
    assert report["v2_is_default_settings"] is True
    assert report["v3_file_tasks_is_default"] is True
    assert report["v3_file_tasks"] == Settings().concurrency.file_tasks
    assert set(report["quality"]["excluded"]) == {"xf-ungrounded", "xf-spam-tools"}
    assert report["v1_demo_default"]["enabled"] is False
    assert report["v1_demo_default"]["tool_calls"] == 0
    assert _v2_settings().model_dump() == Settings().model_dump()
    assert report["default_agent"]["enabled"] is False
    assert report["default_agent"]["tool_calls"] == 0
    assert report["user_version"] == 5
    assert Settings().agent.enabled is False
    assert Settings().agent.tool_protocol == "native"
    assert SqliteStorage(":memory:")._query("PRAGMA user_version")[0][0] == 5  # noqa: SLF001

    by_id = {row["id"]: row for row in report["per_sample"]}
    miss = by_id["xf-l3-miss"]
    assert miss["l3_expected"] == "miss"
    assert miss["v2_hit"] == 0
    assert miss["v3_hit"] == 1
    assert miss["v3_tool_call_ids"]
    assert miss["v3_ids_in_db"] is True
    assert miss["v3_evidence_path_ok"] is True
    assert miss["v3_anchor_observed"] is True
    assert miss["v3_effectiveness"] >= 0.7
    assert miss["v3_repeat_rate"] < 0.3
    hit = by_id["xf-l3-hit"]
    assert hit["l3_expected"] == "hit"
    assert hit["v2_hit"] == 1
    assert hit["v3_hit"] == 1
    assert by_id["xf-l3-miss-search"]["v3_evidence_path_ok"] is True
    assert by_id["xf-l3-miss-search"]["v3_anchor_observed"] is True
    assert by_id["xf-spam-tools"]["v3_effectiveness"] < 0.7
    assert by_id["xf-negative"]["v3_hit"] == 0
    grounded = by_id["xf-grounded"]
    assert grounded["v3_tool_call_ids"] == ["xf-grounded-refs"]
    assert grounded["v3_tool_groundedness"] == 1.0
    # Program-verified TOOL_AGENT evidence is already grounded by the invocation layer.
    assert grounded["v3_needs_evidence"] == 0
    assert grounded["v3_ids_in_db"] is True
    assert grounded["v3_evidence_path_ok"] is True
    assert grounded["v3_anchor_observed"] is True
    ungrounded = by_id["xf-ungrounded"]
    assert ungrounded["v3_hit"] == 1
    assert ungrounded["v3_tool_call_ids"] == []
    assert ungrounded["v3_tool_groundedness"] == 0.0
    assert ungrounded["v3_needs_evidence"] == 0
    assert ungrounded["v3_evidence_path_ok"] is False
    assert ungrounded["v3_anchor_observed"] is False
    assert by_id["xf-negative"]["v3_evidence_path_ok"] is False
    assert by_id["xf-negative"]["v3_anchor_observed"] is False

    ab_by_id = {row["id"]: row for row in report["protocol_ab"]["cases"]}
    miss_ab = ab_by_id["xf-l3-miss"]
    assert miss_ab["native_json_repair"] is False
    assert miss_ab["action_json_json_repair"] is True
    assert miss_ab["action_json_calls"] > miss_ab["native_calls"]
    assert miss_ab["action_json_completed"] is True
    assert miss_ab["action_json_hit"] == 1
    for sid in ("xf-l3-hit", "xf-l3-miss", "xf-l3-miss-search", "xf-grounded"):
        ids = ab_by_id[sid]["action_json_tool_call_ids"]
        assert ids
        assert all(item.startswith("aj-") for item in ids)
        assert ab_by_id[sid]["native_hit"] == ab_by_id[sid]["action_json_hit"]
        assert ab_by_id[sid]["native_tool_groundedness"] == 1.0
        assert ab_by_id[sid]["action_json_tool_groundedness"] == 1.0
        assert ab_by_id[sid]["action_json_ids_in_db"] is True
    assert ab_by_id["xf-ungrounded"]["action_json_tool_call_ids"] == []

    md_path = tmp_path / "v3-d-compare.md"
    write_report(report, json_path=tmp_path / "v3-d-compare.json", markdown_path=md_path)
    text = md_path.read_text(encoding="utf-8")
    assert "## 质量（Finding，macro）" in text
    assert "## 成本（Fake 记账）" in text
    assert "## 延迟（全数据集墙钟）" in text
    assert "## Agent 运行" in text
    assert "## 协议 A/B" in text
    assert "## 脚本门槛" in text
    assert "keep_agent_experimental" in text
    assert "不代表真实模型质量" in text
    assert "xf-ungrounded 不进本表" in text or "不进质量主表" in text
    assert "xf-spam-tools" in text
    assert "cost_usd" in text
    assert "xf-grounded-refs" in text
    assert "hits_preserved" in text
    assert "groundedness_preserved" in text
    assert "aj-" in text
    assert "v2_is_default_settings=True" in text
    assert "v3_file_tasks_is_default=True" in text
    assert "v1_demo" in text
    assert render_markdown(report) == text


def test_v3d_validate_dataset_ok_and_anchor_leak(tmp_path):
    from pathlib import Path

    import yaml
    from reposage.evals.dataset import EvalDataset
    from reposage.evals.v3d_compare import main, validate_dataset

    ds = EvalDataset.load_yaml(Path("reposage/evals/datasets/v3d_cross_file.yaml"))
    result = validate_dataset(ds)
    assert result["status"] == "ok"
    assert result["errors"] == []

    leaked = ds.model_copy(deep=True)
    miss = next(item for item in leaked.samples if item.id == "xf-l3-miss")
    miss.head_files["src/app.py"] = miss.head_files["src/app.py"] + "  # ANCHOR_xf_l3_miss\n"
    bad = validate_dataset(leaked)
    assert bad["status"] == "dataset_invalid"
    assert any(err["code"] == "anchor_leak" and err["id"] == "xf-l3-miss" for err in bad["errors"])

    leak_path = tmp_path / "leaked.yaml"
    leak_path.write_text(
        yaml.safe_dump(leaked.model_dump(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    code = main(
        [
            "--dataset",
            str(leak_path),
            "--output",
            str(tmp_path / "out.json"),
            "--markdown",
            str(tmp_path / "out.md"),
            "--repeats",
            "1",
        ]
    )
    assert code == 1


def test_v3d_validate_dataset_metadata_leak():
    from pathlib import Path

    from reposage.evals.dataset import EvalDataset
    from reposage.evals.v3d_compare import validate_dataset

    ds = EvalDataset.load_yaml(Path("reposage/evals/datasets/v3d_cross_file.yaml"))
    miss = next(item for item in ds.samples if item.id == "xf-l3-miss")
    anchor = "ANCHOR_xf_l3_miss"

    titled = ds.model_copy(deep=True)
    next(item for item in titled.samples if item.id == "xf-l3-miss").pr_title = f"{miss.pr_title} {anchor}"
    titled_bad = validate_dataset(titled)
    assert titled_bad["status"] == "dataset_invalid"
    assert any(
        err["code"] == "anchor_leak" and "pr_title" in err["detail"] for err in titled_bad["errors"]
    )

    described = ds.model_copy(deep=True)
    next(item for item in described.samples if item.id == "xf-l3-miss").pr_description = anchor
    desc_bad = validate_dataset(described)
    assert desc_bad["status"] == "dataset_invalid"
    assert any(
        err["code"] == "anchor_leak" and "pr_description" in err["detail"]
        for err in desc_bad["errors"]
    )

    noted = ds.model_copy(deep=True)
    next(item for item in noted.samples if item.id == "xf-l3-miss").expected[0].note = anchor
    note_bad = validate_dataset(noted)
    assert note_bad["status"] == "dataset_invalid"
    assert any(
        err["code"] == "anchor_leak" and "expected.note" in err["detail"]
        for err in note_bad["errors"]
    )


def test_v3d_evidence_chain_binds_cited_tool_id():
    from reposage.domain.enums import ReviewTaskKind
    from reposage.domain.models import AgentBudget
    from reposage.domain.run import ReviewTask
    from reposage.evals.v3d_compare import _evidence_chain
    from reposage.review.agent.session import AgentSession

    task = ReviewTask(
        task_id="t",
        run_id="r",
        kind=ReviewTaskKind.AGENT_TASK,
        target="src/app.py",
    )
    session = AgentSession(task, file_path="src/app.py", budget=AgentBudget())
    session.add_tool_observation(
        tool_call_id="uncited-read",
        name="read_file",
        content='{"path": "src/util.py", "lines": ["def helper(x):  # ANCHOR_X"]}',
    )
    session.add_tool_observation(
        tool_call_id="cited-other",
        name="read_file",
        content='{"path": "src/app.py", "lines": ["return eval(x)"]}',
    )
    empty = _evidence_chain(
        [session],
        cited_ids=[],
        evidence_path="src/util.py",
        anchor="ANCHOR_X",
    )
    assert empty == (False, False)
    uncited = _evidence_chain(
        [session],
        cited_ids=["cited-other"],
        evidence_path="src/util.py",
        anchor="ANCHOR_X",
    )
    assert uncited == (False, False)
    cited = _evidence_chain(
        [session],
        cited_ids=["uncited-read"],
        evidence_path="src/util.py",
        anchor="ANCHOR_X",
    )
    assert cited == (True, True)

