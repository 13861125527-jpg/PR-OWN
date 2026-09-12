"""V1 single_pass vs V2-A multi_role 脚本化对照（18 §12.4 / T10）。

同一评测集、同一 Fake 计量口径；真实 API 不在本入口运行。
输出 JSON + Markdown 三表：质量 / 成本 / 延迟。
V2-D：本对照钉死 judge.enabled=false、static.enabled=false、symbol_retrieval=false。
V2-E：本对照钉死 review.feedback.enabled=false、review.incremental.enabled=false。
V3-A：本对照钉死 agent.enabled=false。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

from reposage.config.settings import Settings
from reposage.domain.diff import filter_files, parse_unified_diff
from reposage.domain.enums import (
    ChangeRequestSource,
    FindingStatus,
    ReviewRunStatus,
    ReviewStrategyName,
    ReviewTaskStatus,
)
from reposage.domain.models import ChangeRequest, CommitRef, GlobalBudget, ReviewUnit
from reposage.domain.run import ReviewRun
from reposage.domain.strategy import ReviewStrategy, StrategyResult
from reposage.evals.dataset import EvalDataset, EvalSample
from reposage.evals.gate import evaluate_gate_dataset
from reposage.evals.metrics import Metrics, compute_metrics
from reposage.evals.scripted import scripted_candidates
from reposage.providers.git.fake import FakeGitProvider
from reposage.providers.llm.fake import FakeLLMProvider
from reposage.review.context import ContextAssembler
from reposage.review.pipeline import FindingPipeline
from reposage.review.reviewers.roles.multi_role import MultiRoleReviewer
from reposage.review.reviewers.roles.registry import RoleRegistry
from reposage.review.single_pass import SinglePassReviewer

_DEFAULT_DATASET = "reposage/evals/datasets/v1_demo.yaml"
_DEFAULT_JSON = "docs/evidence/v2-a-compare.json"
_DEFAULT_MD = "docs/evidence/v2-a-compare.md"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _avg(ms: list[Metrics], attr: str) -> float:
    return sum(getattr(m, attr) for m in ms) / len(ms) if ms else 0.0


def _avg_position(ms: list[Metrics]) -> float:
    pos = [m.position_accuracy for m in ms if m.details.get("n_expected", 0) > 0]
    return sum(pos) / len(pos) if pos else 0.0


def _budget_rejects(result: StrategyResult) -> int:
    """只以 failed task 为权威来源，避免与 TRUNCATED coverage 重复计数。"""
    n = 0
    for task in result.source_run.tasks:
        err = task.error or ""
        if task.status is ReviewTaskStatus.FAILED and ("预算" in err or "BudgetExceeded" in err):
            n += 1
    return n


async def _units_for(sample: EvalSample) -> tuple[list[ReviewUnit], dict[str, Any], ReviewRun]:
    fake = FakeGitProvider()
    fake.add_snapshot("base", sample.base_files)
    fake.add_snapshot("head", sample.head_files)
    req = ChangeRequest(
        source=ChangeRequestSource.LOCAL_RANGE,
        base=CommitRef(sha="base", label="base"),
        head=CommitRef(sha="head", label="head", locked=True),
        title=sample.pr_title or None,
        description=sample.pr_description or None,
    )
    diff_text = await fake.get_diff("base", "head")
    files = parse_unified_diff(diff_text)
    filtered = filter_files(files, languages=["python"], max_files=40)
    file_map = {f.path: f for f in filtered.kept}
    run = ReviewRun(
        run_id=f"eval-{sample.id}",
        strategy=ReviewStrategyName.SINGLE_PASS,
        status=ReviewRunStatus.RUNNING,
    )
    assembler = ContextAssembler(symbol_retrieval=False)
    units: list[ReviewUnit] = []
    for f in filtered.kept:
        units.extend(assembler.build_file_units(run_id=run.run_id, change_request=req, file=f))
    return units, file_map, run


def _llm_for(sample: EvalSample) -> FakeLLMProvider:
    cands = scripted_candidates(sample.id)
    return FakeLLMProvider(
        default_findings=cands,
        role_findings={"security": [], "correctness": [], "performance": []},
    )


def _v1_strategy(llm: FakeLLMProvider) -> SinglePassReviewer:
    return SinglePassReviewer(llm)


def _v2_strategy(llm: FakeLLMProvider) -> MultiRoleReviewer:
    settings = Settings.model_validate(
        {
            "review": {
                "strategy": "multi_role",
                "roles": ["general", "security", "correctness", "performance"],
                "static": {"enabled": False},
                "judge": {"enabled": False},
                "feedback": {"enabled": False},
                "incremental": {"enabled": False},
            },
            "context": {"symbol_retrieval": False},
            "agent": {"enabled": False},
        }
    )
    return MultiRoleReviewer(llm, RoleRegistry(settings), languages=["python"])


async def _run_side(
    sample: EvalSample,
    strategy: ReviewStrategy,
    run: ReviewRun,
    units: list[ReviewUnit],
    file_map: dict[str, Any],
    llm: FakeLLMProvider,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    result = await strategy.execute(units, run, GlobalBudget())
    elapsed_ms = (time.perf_counter() - t0) * 1000
    pipeline = FindingPipeline(repo="eval-repo", head_sha="head", min_confidence=0.0)
    findings = (await pipeline.process(run_id=run.run_id, candidates=result.candidates, file_map=file_map)).findings
    accepted = [f for f in findings if f.status is FindingStatus.ACCEPTED]
    metrics = compute_metrics(sample.expected, accepted, sample_kind=sample.kind)
    metrics.details["sample_kind"] = sample.kind
    metrics.details["n_expected"] = len(sample.expected)
    usages = result.source_run.usages
    return {
        "metrics": metrics,
        "elapsed_ms": elapsed_ms,
        "model_calls": len(llm.calls),
        "input_tokens": sum(u.input_tokens for u in usages),
        "output_tokens": sum(u.output_tokens for u in usages),
        "budget_rejects": _budget_rejects(result),
        "findings": len(accepted),
    }


async def run_compare(
    dataset_path: str | Path = _DEFAULT_DATASET,
    *,
    repeats: int = 3,
) -> dict[str, Any]:
    ds = EvalDataset.load_yaml(Path(dataset_path))
    v1_pass: list[dict[str, Any]] = []
    v2_pass: list[dict[str, Any]] = []
    v1_totals: list[float] = []
    v2_totals: list[float] = []

    for _ in range(repeats):
        v1_ms = 0.0
        v2_ms = 0.0
        v1_rows: list[dict[str, Any]] = []
        v2_rows: list[dict[str, Any]] = []
        for sample in ds.samples:
            units, file_map, run = await _units_for(sample)
            v1_llm = _llm_for(sample)
            v1_row = await _run_side(sample, _v1_strategy(v1_llm), run, units, file_map, v1_llm)
            v1_ms += v1_row["elapsed_ms"]
            v1_rows.append(v1_row)
            run2 = run.model_copy(update={"strategy": ReviewStrategyName.MULTI_ROLE})
            v2_llm = _llm_for(sample)
            v2_row = await _run_side(sample, _v2_strategy(v2_llm), run2, units, file_map, v2_llm)
            v2_ms += v2_row["elapsed_ms"]
            v2_rows.append(v2_row)
        v1_totals.append(v1_ms)
        v2_totals.append(v2_ms)
        if not v1_pass:
            v1_pass, v2_pass = v1_rows, v2_rows

    v1_metrics = [row["metrics"] for row in v1_pass]
    v2_metrics = [row["metrics"] for row in v2_pass]
    quality = {
        "precision": {"v1": round(_avg(v1_metrics, "precision"), 3), "v2a": round(_avg(v2_metrics, "precision"), 3)},
        "recall": {"v1": round(_avg(v1_metrics, "recall"), 3), "v2a": round(_avg(v2_metrics, "recall"), 3)},
        "f1": {"v1": round(_avg(v1_metrics, "f1"), 3), "v2a": round(_avg(v2_metrics, "f1"), 3)},
        "position_accuracy": {
            "v1": round(_avg_position(v1_metrics), 3),
            "v2a": round(_avg_position(v2_metrics), 3),
        },
        "negative_noise": {
            "v1": float(sum(m.negative_noise for m in v1_metrics)),
            "v2a": float(sum(m.negative_noise for m in v2_metrics)),
        },
    }
    cost = {
        "model_calls": {
            "v1": int(sum(r["model_calls"] for r in v1_pass)),
            "v2a": int(sum(r["model_calls"] for r in v2_pass)),
        },
        "input_tokens": {
            "v1": int(sum(r["input_tokens"] for r in v1_pass)),
            "v2a": int(sum(r["input_tokens"] for r in v2_pass)),
        },
        "output_tokens": {
            "v1": int(sum(r["output_tokens"] for r in v1_pass)),
            "v2a": int(sum(r["output_tokens"] for r in v2_pass)),
        },
        "total_tokens": {
            "v1": int(sum(r["input_tokens"] + r["output_tokens"] for r in v1_pass)),
            "v2a": int(sum(r["input_tokens"] + r["output_tokens"] for r in v2_pass)),
        },
        "budget_rejects": {
            "v1": int(sum(r["budget_rejects"] for r in v1_pass)),
            "v2a": int(sum(r["budget_rejects"] for r in v2_pass)),
        },
        "note": "脚本化 Fake 记账，非真实 API 账单",
    }
    latency = {
        "repeats": repeats,
        "total_ms": {
            "v1": round(statistics.fmean(v1_totals), 2),
            "v2a": round(statistics.fmean(v2_totals), 2),
        },
        "p50_ms": {
            "v1": round(_percentile(v1_totals, 50), 2),
            "v2a": round(_percentile(v2_totals, 50), 2),
        },
        "p95_ms": {
            "v1": round(_percentile(v1_totals, 95), 2),
            "v2a": round(_percentile(v2_totals, 95), 2),
        },
        "note": f"全数据集墙钟，repeats={repeats}；Fake 延迟不代表真实模型",
    }
    gate = {
        rid: {"precision": round(m.precision, 3), "recall": round(m.recall, 3), "tp": m.tp, "fp": m.fp, "fn": m.fn}
        for rid, m in evaluate_gate_dataset().items()
    }
    return {
        "mode": "scripted_fake",
        "dataset": str(dataset_path),
        "samples": len(ds.samples),
        "real_api": "not_run",
        "quality": quality,
        "cost": cost,
        "latency": latency,
        "gate": gate,
    }


def render_markdown(report: dict[str, Any]) -> str:
    q, c, lat = report["quality"], report["cost"], report["latency"]
    lines = [
        "# V2-A vs V1 对照（脚本化 Fake，非真实 API）",
        "",
        "> 由 `python -m reposage.evals.v2a_compare` 生成；原始 JSON：`docs/evidence/v2-a-compare.json`。",
        f"> dataset=`{report['dataset']}` samples={report['samples']} repeats={lat['repeats']} real_api={report['real_api']}",
        "> 脱敏：不含 API Key / 源码原文 / 密钥字面量。",
        "",
        "## 质量（Finding，macro）",
        "",
        "| 指标 | V1 single_pass | V2-A multi_role |",
        "|------|----------------|-----------------|",
        f"| Precision | {q['precision']['v1']} | {q['precision']['v2a']} |",
        f"| Recall | {q['recall']['v1']} | {q['recall']['v2a']} |",
        f"| F1 | {q['f1']['v1']} | {q['f1']['v2a']} |",
        f"| 位置准确率 | {q['position_accuracy']['v1']} | {q['position_accuracy']['v2a']} |",
        f"| 负样本噪声 | {q['negative_noise']['v1']} | {q['negative_noise']['v2a']} |",
        "",
        "脚本化候选经同一 Pipeline。不要求 V2-A 全局 Precision 超过 V1。",
        "质量相同是脚本化候选构造结果：Fake 把相同候选放在 V1 与 V2-A 的 general 角色，其他角色返回空；本表证明执行器/Pipeline/报告可重复，不代表多角色模型效果相同。",
        "",
        "## 成本（Fake 记账）",
        "",
        "| 指标 | V1 single_pass | V2-A multi_role |",
        "|------|----------------|-----------------|",
        f"| 模型调用数 | {c['model_calls']['v1']} | {c['model_calls']['v2a']} |",
        f"| input tokens | {c['input_tokens']['v1']} | {c['input_tokens']['v2a']} |",
        f"| output tokens | {c['output_tokens']['v1']} | {c['output_tokens']['v2a']} |",
        f"| total tokens | {c['total_tokens']['v1']} | {c['total_tokens']['v2a']} |",
        f"| 预算拒绝次数 | {c['budget_rejects']['v1']} | {c['budget_rejects']['v2a']} |",
        "",
        c["note"] + "。",
        "",
        "## 延迟（全数据集墙钟）",
        "",
        "| 指标 | V1 single_pass | V2-A multi_role |",
        "|------|----------------|-----------------|",
        f"| total_ms（均值） | {lat['total_ms']['v1']} | {lat['total_ms']['v2a']} |",
        f"| p50_ms | {lat['p50_ms']['v1']} | {lat['p50_ms']['v2a']} |",
        f"| p95_ms | {lat['p95_ms']['v1']} | {lat['p95_ms']['v2a']} |",
        "",
        lat["note"] + "。",
        "",
        "## 辅助：Gate Precision/Recall（不替代 Finding 质量表）",
        "",
        "| 角色 | Precision | Recall | TP/FP/FN |",
        "|------|-----------|--------|----------|",
    ]
    for role_id, row in report["gate"].items():
        lines.append(
            f"| {role_id} | {row['precision']} | {row['recall']} | {row['tp']}/{row['fp']}/{row['fn']} |"
        )
    lines.extend(["", "真实 API 对照：未运行。", ""])
    return "\n".join(lines)


def write_report(
    report: dict[str, Any],
    *,
    json_path: str | Path = _DEFAULT_JSON,
    markdown_path: str | Path = _DEFAULT_MD,
) -> None:
    json_file = Path(json_path)
    md_file = Path(markdown_path)
    json_file.parent.mkdir(parents=True, exist_ok=True)
    json_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_file.write_text(render_markdown(report), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="V1 vs V2-A 脚本化对照")
    parser.add_argument("--dataset", default=_DEFAULT_DATASET)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", default=_DEFAULT_JSON)
    parser.add_argument("--markdown", default=_DEFAULT_MD)
    args = parser.parse_args()
    report = asyncio.run(run_compare(args.dataset, repeats=args.repeats))
    write_report(report, json_path=args.output, markdown_path=args.markdown)
    print(f"wrote {args.output} and {args.markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
