"""评测指标（evals/metrics.py）。

口径见 11 §5：Precision / Recall / F1 / 位置准确率 / 无缺陷噪声。

P1-1：一对一匹配——每个 Finding 最多命中一条 expected（匹配后消耗），
保证 hits ≤ min(len(expected), len(findings))；category 必须一致；
severity 暂不参与匹配（由标注单独核对）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from reposage.domain.enums import FindingCategory


@dataclass
class Metrics:
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    position_accuracy: float = 0.0
    negative_noise: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_report(self) -> str:
        return (
            f"Precision={self.precision:.3f} Recall={self.recall:.3f} F1={self.f1:.3f} "
            f"PositionAcc={self.position_accuracy:.3f} NegativeNoise={self.negative_noise}"
        )


def _normalize_category(cat: str | FindingCategory | None) -> str | None:
    if cat is None:
        return None
    return cat.value if isinstance(cat, FindingCategory) else str(cat)


def _is_hit(
    finding_path: str | None,
    finding_line: int | None,
    expected_path: str | None,
    expected_line: int | None,
) -> bool:
    """命中 = 路径一致（期望路径为空则不比较）且行号一致（期望行号为空则只比路径）。"""
    if expected_path and finding_path != expected_path:
        return False
    return not (expected_line is not None and finding_line != expected_line)


def compute_metrics(
    expected: list[Any],
    findings: list[Any],
    *,
    sample_kind: str = "single_defect",
) -> Metrics:
    """对单个样本计算指标（P1-1：一对一匹配）。

    expected: list[ExpectedFinding]
    findings: list[Finding]（accepted 状态）
    """
    used = [False] * len(findings)
    hits = 0
    for exp in expected:
        exp_cat = _normalize_category(getattr(exp, "category", None))
        for i, f in enumerate(findings):
            if used[i]:
                continue
            # category 必须一致（P1-1）
            if exp_cat is not None and _normalize_category(f.category) != exp_cat:
                continue
            if _is_hit(f.canonical_path, f.canonical_start_line, exp.path, exp.line):
                used[i] = True
                hits += 1
                break  # 每个 Finding 只消耗一次

    recall = hits / len(expected) if expected else 1.0
    precision = hits / len(findings) if findings else (1.0 if not expected else 0.0)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    position_ok = 0
    for f in findings:
        if f.canonical_path and f.canonical_start_line is not None:
            position_ok += 1
    position_accuracy = position_ok / len(findings) if findings else 0.0

    negative_noise = len(findings) if sample_kind == "negative" else 0

    return Metrics(
        precision=precision,
        recall=recall,
        f1=f1,
        position_accuracy=position_accuracy,
        negative_noise=negative_noise,
    )
