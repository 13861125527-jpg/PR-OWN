"""V2-A 门控评测（18 §12）：纯函数 Precision/Recall，general 不计入。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from reposage.domain.diff import parse_unified_diff
from reposage.review.reviewers.roles.gates import evaluate_gate, extract_features
from reposage.review.reviewers.roles.registry import BUILTIN_ROLE_SPECS

OPTIONAL_ROLES = ("security", "correctness", "performance")
GATE_THRESHOLDS: dict[str, tuple[float, float]] = {
    "security": (0.6, 0.8),
    "correctness": (0.7, 0.7),
    "performance": (0.8, 0.6),
}
DEFAULT_DATASET = Path(__file__).resolve().parent / "datasets" / "v2a_gate.yaml"


@dataclass
class GateEvalSample:
    id: str
    diff: str
    expected_roles: list[str]
    language: str = "python"


@dataclass
class RoleGateMetrics:
    role_id: str
    precision: float
    recall: float
    tp: int
    fp: int
    fn: int
    misses: list[str] = field(default_factory=list)


def load_gate_dataset(path: Path | None = None) -> list[GateEvalSample]:
    data: dict[str, Any] = yaml.safe_load((path or DEFAULT_DATASET).read_text(encoding="utf-8"))
    samples: list[GateEvalSample] = []
    for raw in data["samples"]:
        samples.append(
            GateEvalSample(
                id=str(raw["id"]),
                diff=str(raw["diff"]),
                expected_roles=list(raw.get("expected_roles") or []),
                language=str(raw.get("language") or "python"),
            )
        )
    return samples


def evaluate_gate_dataset(
    samples: list[GateEvalSample] | None = None,
    *,
    languages: list[str] | None = None,
) -> dict[str, RoleGateMetrics]:
    samples = samples if samples is not None else load_gate_dataset()
    languages = languages or ["python"]
    specs = {spec.id: spec for spec in BUILTIN_ROLE_SPECS if spec.id in OPTIONAL_ROLES}
    tallies: dict[str, dict[str, int]] = {
        rid: {"tp": 0, "fp": 0, "fn": 0} for rid in OPTIONAL_ROLES
    }
    misses: dict[str, list[str]] = {rid: [] for rid in OPTIONAL_ROLES}

    for sample in samples:
        files = parse_unified_diff(sample.diff)
        enabled: set[str] = set()
        hit_features: dict[str, list[str]] = {}
        for file in files:
            if file.language is None:
                file.language = sample.language
            features = extract_features(file)
            for role_id, spec in specs.items():
                decision = evaluate_gate(features, spec, languages)
                if decision.enabled:
                    enabled.add(role_id)
                    hit_features[role_id] = list(decision.matched_features)
        expected = set(sample.expected_roles)
        for role_id in OPTIONAL_ROLES:
            want = role_id in expected
            got = role_id in enabled
            if want and got:
                tallies[role_id]["tp"] += 1
            elif got and not want:
                tallies[role_id]["fp"] += 1
            elif want and not got:
                tallies[role_id]["fn"] += 1
                misses[role_id].append(f"{sample.id}:{','.join(hit_features.get(role_id, [])) or 'none'}")

    result: dict[str, RoleGateMetrics] = {}
    for role_id in OPTIONAL_ROLES:
        tp, fp, fn = tallies[role_id]["tp"], tallies[role_id]["fp"], tallies[role_id]["fn"]
        precision = tp / (tp + fp) if (tp + fp) else 1.0
        recall = tp / (tp + fn) if (tp + fn) else 1.0
        result[role_id] = RoleGateMetrics(
            role_id=role_id,
            precision=precision,
            recall=recall,
            tp=tp,
            fp=fp,
            fn=fn,
            misses=misses[role_id],
        )
    return result


__all__ = [
    "DEFAULT_DATASET",
    "GATE_THRESHOLDS",
    "OPTIONAL_ROLES",
    "GateEvalSample",
    "RoleGateMetrics",
    "evaluate_gate_dataset",
    "load_gate_dataset",
]
