"""评测指标测试。"""

from reposage.domain.enums import FindingCategory, Severity
from reposage.domain.finding import Finding
from reposage.evals.dataset import EvalDataset, ExpectedFinding
from reposage.evals.metrics import compute_metrics


def _finding(path: str | None, line: int | None) -> Finding:
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
