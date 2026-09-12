"""L3-off vs L3-on 脚本化对照（19 §12.2）。

同一评测集、同一 Fake、同一 single_pass；真实 API 不在本入口运行。
主质量证据是检索命中率；Finding 三表披露 Fake 限制。
V2-C：本对照不启用静态分析（不经 ReviewService.static；static.enabled 默认 False）。
V2-D：FindingPipeline.judge_enabled 默认 False。
V3-A：agent.enabled 默认 False，本对照不启用 Agent。
V2-E：本对照不启用反馈匹配与增量 FETCH（不经 ReviewService；feedback 默认空列表）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

from reposage.domain.diff import filter_files, parse_unified_diff
from reposage.domain.enums import (
    ChangeRequestSource,
    ContextLayer,
    FindingStatus,
    ReviewRunStatus,
    ReviewStrategyName,
)
from reposage.domain.models import ChangeRequest, CommitRef, GlobalBudget, ReviewUnit
from reposage.domain.run import ReviewRun
from reposage.evals.dataset import EvalDataset, EvalSample
from reposage.evals.l3 import evaluate_l3_dataset
from reposage.evals.metrics import compute_metrics
from reposage.evals.scripted import scripted_candidates
from reposage.evals.v2a_compare import _avg, _avg_position, _budget_rejects, _percentile
from reposage.providers.git.fake import FakeGitProvider
from reposage.providers.llm.fake import FakeLLMProvider
from reposage.review.context import ContextAssembler, estimate_tokens, unit_to_messages
from reposage.review.pipeline import FindingPipeline
from reposage.review.single_pass import SinglePassReviewer
from reposage.review.symbols.snapshot import HeadSnapshot

_DEFAULT_DATASET = "reposage/evals/datasets/v2b_compare.yaml"
_DEFAULT_JSON = "docs/evidence/v2-b-compare.json"
_DEFAULT_MD = "docs/evidence/v2-b-compare.md"


async def _units_for(
    sample: EvalSample, *, symbol_retrieval: bool
) -> tuple[list[ReviewUnit], dict[str, Any], ReviewRun]:
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
    assembler = ContextAssembler(symbol_retrieval=symbol_retrieval)
    snapshot = HeadSnapshot.from_blobs("head", sample.head_files) if symbol_retrieval else None
    units: list[ReviewUnit] = []
    for f in filtered.kept:
        units.extend(
            assembler.build_file_units(
                run_id=run.run_id, change_request=req, file=f, snapshot=snapshot
            )
        )
    return units, file_map, run


async def _run_side(
    sample: EvalSample,
    *,
    symbol_retrieval: bool,
) -> dict[str, Any]:
    units, file_map, run = await _units_for(sample, symbol_retrieval=symbol_retrieval)
    require = "def helper" if sample.id == "v2b-consume" else None
    llm = FakeLLMProvider(
        default_findings=scripted_candidates(sample.id),
        require_content=require,
    )
    t0 = time.perf_counter()
    result = await SinglePassReviewer(llm).execute(units, run, GlobalBudget())
    elapsed_ms = (time.perf_counter() - t0) * 1000
    pipeline = FindingPipeline(repo="eval-repo", head_sha="head", min_confidence=0.0)
    findings = (await pipeline.process(run_id=run.run_id, candidates=result.candidates, file_map=file_map)).findings
    accepted = [f for f in findings if f.status is FindingStatus.ACCEPTED]
    metrics = compute_metrics(sample.expected, accepted, sample_kind=sample.kind)
    metrics.details["sample_kind"] = sample.kind
    metrics.details["n_expected"] = len(sample.expected)
    estimated = sum(
        estimate_tokens(str(m.get("content", "")))
        for unit in units
        for m in unit_to_messages(unit)
    )
    return {
        "metrics": metrics,
        "elapsed_ms": elapsed_ms,
        "model_calls": len(llm.calls),
        "input_tokens": estimated,
        "output_tokens": sum(u.output_tokens for u in result.source_run.usages),
        "budget_rejects": _budget_rejects(result),
        "l3_chunks": sum(
            1 for u in units for c in u.context.chunks if c.layer is ContextLayer.L3
        ),
    }


async def run_compare(
    dataset_path: str | Path = _DEFAULT_DATASET,
    *,
    repeats: int = 3,
) -> dict[str, Any]:
    ds = EvalDataset.load_yaml(Path(dataset_path))
    off_pass: list[dict[str, Any]] = []
    on_pass: list[dict[str, Any]] = []
    off_totals: list[float] = []
    on_totals: list[float] = []
    for _ in range(repeats):
        off_ms = 0.0
        on_ms = 0.0
        off_rows: list[dict[str, Any]] = []
        on_rows: list[dict[str, Any]] = []
        for sample in ds.samples:
            off_row = await _run_side(sample, symbol_retrieval=False)
            on_row = await _run_side(sample, symbol_retrieval=True)
            off_ms += off_row["elapsed_ms"]
            on_ms += on_row["elapsed_ms"]
            off_rows.append(off_row)
            on_rows.append(on_row)
        off_totals.append(off_ms)
        on_totals.append(on_ms)
        if not off_pass:
            off_pass, on_pass = off_rows, on_rows
    off_m = [r["metrics"] for r in off_pass]
    on_m = [r["metrics"] for r in on_pass]
    l3 = evaluate_l3_dataset()
    return {
        "mode": "scripted_fake",
        "dataset": str(dataset_path),
        "samples": len(ds.samples),
        "real_api": "not_run",
        "quality": {
            "precision": {"off": round(_avg(off_m, "precision"), 3), "on": round(_avg(on_m, "precision"), 3)},
            "recall": {"off": round(_avg(off_m, "recall"), 3), "on": round(_avg(on_m, "recall"), 3)},
            "f1": {"off": round(_avg(off_m, "f1"), 3), "on": round(_avg(on_m, "f1"), 3)},
            "position_accuracy": {
                "off": round(_avg_position(off_m), 3),
                "on": round(_avg_position(on_m), 3),
            },
            "note": "脚本化 Fake 把候选放在 L3-off/on；v2b-consume 仅当消息含 helper 定义才吐 Finding，证明管道吃到 L3，不代表真实模型增益。",
        },
        "cost": {
            "model_calls": {
                "off": int(sum(r["model_calls"] for r in off_pass)),
                "on": int(sum(r["model_calls"] for r in on_pass)),
            },
            "input_tokens": {
                "off": int(sum(r["input_tokens"] for r in off_pass)),
                "on": int(sum(r["input_tokens"] for r in on_pass)),
            },
            "output_tokens": {
                "off": int(sum(r["output_tokens"] for r in off_pass)),
                "on": int(sum(r["output_tokens"] for r in on_pass)),
            },
            "l3_chunks": {
                "off": int(sum(r["l3_chunks"] for r in off_pass)),
                "on": int(sum(r["l3_chunks"] for r in on_pass)),
            },
            "budget_rejects": {
                "off": int(sum(r["budget_rejects"] for r in off_pass)),
                "on": int(sum(r["budget_rejects"] for r in on_pass)),
            },
            "note": "input tokens 来自 ReviewUnit 消息的 estimate_tokens，不是 Fake 固定 100。",
        },
        "latency": {
            "repeats": repeats,
            "total_ms": {
                "off": round(statistics.fmean(off_totals), 2),
                "on": round(statistics.fmean(on_totals), 2),
            },
            "p50_ms": {
                "off": round(_percentile(off_totals, 50), 2),
                "on": round(_percentile(on_totals, 50), 2),
            },
            "p95_ms": {
                "off": round(_percentile(off_totals, 95), 2),
                "on": round(_percentile(on_totals, 95), 2),
            },
            "note": f"全数据集墙钟，repeats={repeats}；Fake 延迟不代表真实模型",
        },
        "retrieval": {
            "dataset": "reposage/evals/datasets/v2b_l3.yaml",
            "hit_rate": round(l3.hit_rate, 3),
            "false_rate": round(l3.false_rate, 3),
            "tp": l3.tp,
            "fn": l3.fn,
            "fp": l3.fp,
            "expected_total": l3.expected_total,
            "forbidden_total": l3.forbidden_total,
            "note": "按 expected/forbidden symbol 计数，不是按样本。",
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    q, c, lat, r = report["quality"], report["cost"], report["latency"], report["retrieval"]
    return "\n".join(
        [
            "# V2-B L3-off vs L3-on 对照（脚本化 Fake，非真实 API）",
            "",
            "> 由 `python -m reposage.evals.v2b_compare` 生成；原始 JSON：`docs/evidence/v2-b-compare.json`。",
            f"> dataset=`{report['dataset']}` samples={report['samples']} repeats={lat['repeats']} real_api={report['real_api']}",
            "> 脱敏：不含 API Key / 源码原文 / 密钥字面量。",
            "",
            "## 检索命中（程序指标，主验收）",
            "",
            f"dataset=`{r.get('dataset', 'reposage/evals/datasets/v2b_l3.yaml')}` "
            f"hit_rate={r['hit_rate']} false_rate={r['false_rate']} "
            f"TP/FN/FP={r['tp']}/{r['fn']}/{r['fp']} "
            f"expected={r.get('expected_total', '?')} forbidden={r.get('forbidden_total', '?')}",
            "",
            r.get("note", "按 symbol 计数。"),
            "",
            "## 质量（Finding，macro）",
            "",
            "| 指标 | L3-off | L3-on |",
            "|------|--------|-------|",
            f"| Precision | {q['precision']['off']} | {q['precision']['on']} |",
            f"| Recall | {q['recall']['off']} | {q['recall']['on']} |",
            f"| F1 | {q['f1']['off']} | {q['f1']['on']} |",
            f"| 位置准确率 | {q['position_accuracy']['off']} | {q['position_accuracy']['on']} |",
            "",
            q["note"],
            "",
            "## 成本（Fake 记账）",
            "",
            "| 指标 | L3-off | L3-on |",
            "|------|--------|-------|",
            f"| 模型调用数 | {c['model_calls']['off']} | {c['model_calls']['on']} |",
            f"| input tokens | {c['input_tokens']['off']} | {c['input_tokens']['on']} |",
            f"| output tokens | {c['output_tokens']['off']} | {c['output_tokens']['on']} |",
            f"| L3 chunks | {c['l3_chunks']['off']} | {c['l3_chunks']['on']} |",
            f"| 预算拒绝次数 | {c['budget_rejects']['off']} | {c['budget_rejects']['on']} |",
            "",
            c["note"].rstrip("。") + "。",
            "",
            "## 延迟（全数据集墙钟）",
            "",
            "| 指标 | L3-off | L3-on |",
            "|------|--------|-------|",
            f"| total_ms（均值） | {lat['total_ms']['off']} | {lat['total_ms']['on']} |",
            f"| p50_ms | {lat['p50_ms']['off']} | {lat['p50_ms']['on']} |",
            f"| p95_ms | {lat['p95_ms']['off']} | {lat['p95_ms']['on']} |",
            "",
            lat["note"] + "。",
            "",
            "真实 API 对照：未运行。",
            "",
        ]
    )


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
    parser = argparse.ArgumentParser(description="L3-off vs L3-on 脚本化对照")
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
