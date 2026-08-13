"""评测指标测试（P1-1：一对一匹配、category 必须一致、precision ≤ 1）。"""

from reposage.domain.enums import FindingCategory, Severity
from reposage.domain.finding import Finding
from reposage.evals.dataset import EvalDataset, ExpectedFinding
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
