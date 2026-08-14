"""评测门槛（evals/thresholds.py，V1-d DoD：低于门槛 → 非零退出码）。

验收门槛（11 §5 / V1-d DoD）：
- Precision ≥ 0.7（macro：各样本平均）；
- 位置准确率 ≥ 0.8（macro）；
- 无缺陷 PR 噪声 ≤ 1 条/PR（对 negative 样本取最大 NegativeNoise）。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from reposage.domain.finding import FindingCandidate
from reposage.evals.dataset import EvalDataset
from reposage.evals.metrics import Metrics
from reposage.evals.runner import EvalRunner
from reposage.evals.scripted import ScriptedStrategy, scripted_candidates


@dataclass
class ThresholdConfig:
    precision: float = 0.7
    position_accuracy: float = 0.8
    max_negative_noise: int = 1
    failures: list[str] = field(default_factory=list)


def check_thresholds(results: dict[str, Metrics], config: ThresholdConfig | None = None) -> tuple[bool, list[str]]:
    """汇总门槛检查（macro 平均）。返回 (ok, violations)。"""
    cfg = config or ThresholdConfig()
    if not results:
        return False, ["无评测结果"]

    precisions = [m.precision for m in results.values()]
    # 位置准确率只对有 expected 的样本聚合（无位置要求者 1.0 会拉高 macro，P1-2）
    positions = [
        m.position_accuracy
        for m in results.values()
        if m.details.get("n_expected", 0) > 0
    ]
    negatives = [
        m.negative_noise
        for m in results.values()
        if m.details.get("sample_kind") == "negative"
    ]

    avg_precision = sum(precisions) / len(precisions)
    avg_position = sum(positions) / len(positions) if positions else 1.0  # 无 positive 样本则无位置要求
    max_noise = max(negatives) if negatives else 0

    violations: list[str] = []
    if avg_precision < cfg.precision:
        violations.append(f"Precision {avg_precision:.3f} < {cfg.precision}")
    if avg_position < cfg.position_accuracy:
        violations.append(f"PositionAcc {avg_position:.3f} < {cfg.position_accuracy}")
    if max_noise > cfg.max_negative_noise:
        violations.append(f"NegativeNoise {max_noise} > {cfg.max_negative_noise}/PR")
    return not violations, violations


def run_scripted_gate(
    dataset_path: str,
    *,
    scripted: Callable[[str], list[FindingCandidate]] = scripted_candidates,
    min_confidence: float = 0.0,
) -> tuple[bool, dict[str, Metrics], list[str]]:
    """脚本化 Fake 评测 + 门槛检查（CI 用）。"""
    import asyncio
    from pathlib import Path

    ds = EvalDataset.load_yaml(Path(dataset_path))
    runner = EvalRunner(ds, strategy=ScriptedStrategy(scripted), min_confidence=min_confidence)
    asyncio.run(runner.run())
    ok, violations = check_thresholds(runner.results)
    return ok, runner.results, violations


def main() -> int:
    dataset_path = sys.argv[1] if len(sys.argv) > 1 else "reposage/evals/datasets/v1_demo.yaml"
    ok, results, violations = run_scripted_gate(dataset_path)
    print(f"Evals: {len(results)} samples")
    for sid, m in results.items():
        print(f"  {sid}: {m.to_report()}")
    if not ok:
        print("门槛未达标：")
        for v in violations:
            print(f"  FAIL {v}")
        return 1
    print("门槛全部达标")
    return 0


if __name__ == "__main__":
    sys.exit(main())
