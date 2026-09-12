"""V2-B L3 检索命中率评测（程序指标，不靠模型）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from reposage.domain.diff import parse_unified_diff
from reposage.review.symbols.retrieve import collect_l3_hits
from reposage.review.symbols.snapshot import HeadSnapshot

DEFAULT_DATASET = Path(__file__).resolve().parent / "datasets" / "v2b_l3.yaml"
HIT_RATE_MIN = 0.8
FALSE_RATE_MAX = 0.1


@dataclass
class L3EvalSample:
    id: str
    diff: str
    head_files: dict[str, str]
    expected_symbols: list[str] = field(default_factory=list)
    forbidden_symbols: list[str] = field(default_factory=list)
    language: str = "python"


@dataclass
class L3Metrics:
    hit_rate: float
    false_rate: float
    tp: int
    fn: int
    fp: int
    expected_total: int
    forbidden_total: int
    misses: list[str]


def load_l3_dataset(path: Path | None = None) -> list[L3EvalSample]:
    data: dict[str, Any] = yaml.safe_load((path or DEFAULT_DATASET).read_text(encoding="utf-8"))
    samples: list[L3EvalSample] = []
    for raw in data["samples"]:
        samples.append(
            L3EvalSample(
                id=str(raw["id"]),
                diff=str(raw["diff"]),
                head_files={str(k): str(v) for k, v in (raw.get("head_files") or {}).items()},
                expected_symbols=list(raw.get("expected_symbols") or []),
                forbidden_symbols=list(raw.get("forbidden_symbols") or []),
                language=str(raw.get("language") or "python"),
            )
        )
    return samples


def _names(hits: list[Any]) -> set[str]:
    out: set[str] = set()
    for hit in hits:
        out.add(hit.symbol.name)
        out.add(hit.symbol.qualname)
    return out


def evaluate_l3_dataset(samples: list[L3EvalSample] | None = None) -> L3Metrics:
    samples = samples if samples is not None else load_l3_dataset()
    tp = fn = fp = 0
    misses: list[str] = []
    expected_total = 0
    forbidden_total = 0
    for sample in samples:
        files = parse_unified_diff(sample.diff)
        if not files:
            continue
        file = files[0]
        file.language = sample.language
        snap = HeadSnapshot.from_blobs("head", sample.head_files)
        hits = collect_l3_hits(file, snap).hits
        got = _names(hits)
        for name in sample.expected_symbols:
            expected_total += 1
            if name in got:
                tp += 1
            else:
                fn += 1
                misses.append(f"{sample.id}:fn:{name}")
        for name in sample.forbidden_symbols:
            forbidden_total += 1
            if name in got:
                fp += 1
                misses.append(f"{sample.id}:fp:{name}")
    hit_rate = tp / expected_total if expected_total else 1.0
    false_rate = fp / forbidden_total if forbidden_total else 0.0
    return L3Metrics(
        hit_rate=hit_rate,
        false_rate=false_rate,
        tp=tp,
        fn=fn,
        fp=fp,
        expected_total=expected_total,
        forbidden_total=forbidden_total,
        misses=misses,
    )
