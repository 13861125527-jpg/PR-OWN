"""Run a resumable real-model Base vs V3 agent comparison on one fixed dataset.

Two independent semantic Judge calls score accepted findings against the same
gold answers; their agreement is reported so disagreement is not hidden.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import statistics
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from reposage.config.settings import Settings
from reposage.domain.enums import FindingStatus, ReviewTaskStatus
from reposage.evals.dataset import EvalDataset, EvalSample
from reposage.evals.metrics import compute_metrics
from reposage.evals.real_v2 import _diagnostics, _json_default, _metrics_dict
from reposage.evals.semantic_judge import (
    SemanticJudgeConfig,
    compute_semantic_metrics,
    finding_summary,
)
from reposage.observability.logging import StructuredLogger
from reposage.providers.git.fake import FakeGitProvider
from reposage.providers.llm.openai_compat import OpenAICompatProvider, resolve_credentials
from reposage.review.agent import reviewer as agent_reviewer
from reposage.review.agent.session import AgentSession
from reposage.review.service import ReviewService
from reposage.storage.sqlite import SqliteStorage

DATASET = "reposage/evals/datasets/resume_l3_context_30.yaml"
OUTPUT = "docs/evidence/resume-v3-real-compare-30.json"
MARKDOWN = "docs/evidence/resume-v3-real-compare-30.md"
CHECKPOINT = "docs/evidence/resume-v3-real-compare-30.checkpoint.json"


def _redact(value: str) -> str:
    return re.sub(r"(?i)(sk-[a-z0-9_-]{4,}|bearer\s+[a-z0-9._-]{4,})", "<redacted>", value)[:240]


@contextmanager
def _capture_sessions() -> Iterator[list[AgentSession]]:
    captured: list[AgentSession] = []
    original = agent_reviewer.run_agent_loop  # type: ignore[attr-defined]

    async def wrapped(session: AgentSession, *args: Any, **kwargs: Any) -> Any:
        captured.append(session)
        return await original(session, *args, **kwargs)

    agent_reviewer.run_agent_loop = wrapped  # type: ignore[attr-defined]
    try:
        yield captured
    finally:
        agent_reviewer.run_agent_loop = original  # type: ignore[attr-defined]


def _settings(model: str, *, agentic: bool) -> Settings:
    base = Settings()
    return base.model_copy(update={
        "llm": base.llm.model_copy(update={"model": model, "max_output_tokens": 8000}),
        "review": base.review.model_copy(update={
            "strategy": "agentic" if agentic else "single_pass",
            "min_confidence": 0.0,
            "static": base.review.static.model_copy(update={"enabled": False}),
            "judge": base.review.judge.model_copy(update={"enabled": False}),
            "feedback": base.review.feedback.model_copy(update={"enabled": False}),
            "incremental": base.review.incremental.model_copy(update={"enabled": False}),
        }),
        "context": base.context.model_copy(update={"symbol_retrieval": True}),
        "agent": base.agent.model_copy(update={
            "enabled": agentic, "tool_protocol": "native", "max_rounds": 12,
            "max_tool_calls": 16, "max_wallclock_s": 300,
        }),
        "budget": base.budget.model_copy(update={
            "max_total_tokens": 120000, "max_cost_usd": 20.0, "max_runtime_seconds": 900,
        }),
        "concurrency": base.concurrency.model_copy(update={"file_tasks": 1, "model_requests": 1}),
    })


def _db_rows(store: SqliteStorage, table: str, run_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in store._query(f"SELECT * FROM {table} WHERE run_id = ?", (run_id,))]  # noqa: S608, SLF001


async def _arm(sample: EvalSample, provider: OpenAICompatProvider, *, model: str, agentic: bool) -> dict[str, Any]:
    git = FakeGitProvider(repository_id=f"v3-live-{sample.id}")
    git.add_snapshot("base", sample.base_files)
    git.add_snapshot("head", sample.head_files)
    git.add_pr(1, base="base", head="head", title=sample.pr_title, description=sample.pr_description)
    store = SqliteStorage(":memory:")
    settings = _settings(model, agentic=agentic)
    service = ReviewService(git, provider, store, settings=settings, logger=StructuredLogger(sink=io.StringIO()))
    started = time.perf_counter()
    sessions: list[AgentSession] = []
    if agentic:
        with _capture_sessions() as sessions:
            run, findings = await service.review("1")
    else:
        run, findings = await service.review("1")
    accepted = [item for item in findings if item.status is FindingStatus.ACCEPTED]
    usages = _db_rows(store, "usages", run.run_id)
    tasks = _db_rows(store, "tasks", run.run_id)
    tool_calls = store._query(
        "SELECT tc.* FROM tool_calls tc JOIN tasks t ON t.task_id=tc.task_id WHERE t.run_id=?",
        (run.run_id,),
    )
    metrics = compute_metrics(sample.expected, accepted, sample_kind=sample.kind)
    return {
        "metrics": _metrics_dict(metrics),
        "diagnostics": _diagnostics(sample, accepted),
        "findings": [finding_summary(item) for item in accepted],
        "all_findings": [
            {**finding_summary(item), "status": item.status.value, "needs_evidence": item.needs_evidence}
            for item in findings
        ],
        "finding_objects": accepted,
        "expected_count": len(sample.expected),
        "candidate_count": len(findings),
        "accepted_count": len(accepted),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "usage": {
            "logical_invocations": len(usages),
            "input_tokens": sum(int(row["in_tokens"]) for row in usages),
            "output_tokens": sum(int(row["out_tokens"]) for row in usages),
            "provider_latency_ms": sum(int(row["latency_ms"]) for row in usages),
            "retries": sum(int(row["retries"]) for row in usages),
            "schema_repairs": sum(int(row["schema_repairs"]) for row in usages),
        },
        "product_judge_calls": sum(row["kind"] == "judge_adjudicate" for row in tasks),
        "tool_calls": len(tool_calls),
        "tool_attempts": sum(session.tool_attempts for session in sessions),
        "successful_tools": sum(session.successful_tools for session in sessions),
        "repeat_count": sum(session.repeat_count for session in sessions),
        "agent_partial_or_failed": sum(
            session.status in {ReviewTaskStatus.PARTIAL, ReviewTaskStatus.FAILED} for session in sessions
        ),
        "agent_sessions": [
            {"status": session.status.value, "stop_reason": session.stop_reason,
             "candidate_count": len(session.candidates), "rounds": session.rounds_used}
            for session in sessions
        ],
        "task_failures": sum(row["status"] in {"failed", "partial", "cancelled"} for row in tasks),
        "warnings": [_redact(item) for item in run.warnings],
    }


async def _score_judges(sample: EvalSample, arm: dict[str, Any], findings: list[Any], judges: list[OpenAICompatProvider], cfg: SemanticJudgeConfig) -> None:
    rows = []
    for provider in judges:
        metric = await compute_semantic_metrics(
            sample.expected, findings, provider, line_tolerance=5,
            temperature=cfg.temperature, max_tokens=cfg.max_output_tokens,
        )
        rows.append(metric.model_dump(mode="json"))
    arm["semantic_judges"] = rows
    arm["judge_agreement"] = rows[0]["matched"] == rows[1]["matched"] and {
        d["pair_id"]: d["semantically_equivalent"] for d in rows[0]["decisions"]
    } == {d["pair_id"]: d["semantically_equivalent"] for d in rows[1]["decisions"]}
    arm.pop("finding_objects", None)


async def _sample(sample: EvalSample, provider: OpenAICompatProvider, judges: list[OpenAICompatProvider], cfg: SemanticJudgeConfig, model: str) -> dict[str, Any]:
    row: dict[str, Any] = {"id": sample.id, "kind": sample.kind}
    for name, agentic in (("base", False), ("v3", True)):
        arm = await _arm(sample, provider, model=model, agentic=agentic)
        findings = list(arm["finding_objects"])
        await _score_judges(sample, arm, findings, judges, cfg)
        row[name] = arm
    return row


def _micro(rows: list[dict[str, Any]], arm: str, judge_index: int) -> dict[str, Any]:
    selected = [row[arm]["semantic_judges"][judge_index] for row in rows]
    matched = sum(row["matched"] for row in selected)
    expected = sum(row["expected"] for row in selected)
    reported = sum(row["reported"] for row in selected)
    precision = matched / reported if reported else (1.0 if not expected else 0.0)
    recall = matched / expected if expected else 1.0
    return {"matched": matched, "expected": expected, "reported": reported,
            "precision": round(precision, 3), "recall": round(recall, 3),
            "f1": round(2 * precision * recall / (precision + recall), 3) if precision + recall else 0.0,
            "perfect_samples": sum(r["precision"] == 1 and r["recall"] == 1 for r in selected)}


def _aggregate(rows: list[dict[str, Any]], model: str, judge_model: str) -> dict[str, Any]:
    report: dict[str, Any] = {"version": "V3-live", "model": model, "judge_model": judge_model,
        "dataset": DATASET, "sample_count": len(rows), "execution_completed": all(
            "base" in r and "v3" in r and not r["base"]["task_failures"] and not r["v3"]["task_failures"]
            for r in rows
        ),
        "arms": {}, "per_sample": rows}
    for arm in ("base", "v3"):
        attempts = sum(row[arm]["tool_attempts"] for row in rows)
        successful = sum(row[arm]["successful_tools"] for row in rows)
        repeats = sum(row[arm]["repeat_count"] for row in rows)
        diagnostics = [row[arm]["diagnostics"] for row in rows]
        expected_total = sum(row[arm]["expected_count"] for row in rows)
        report["arms"][arm] = {
            "judge_1": _micro(rows, arm, 0), "judge_2": _micro(rows, arm, 1),
            "judge_agreement_samples": sum(row[arm]["judge_agreement"] for row in rows),
            "avg_latency_ms": round(statistics.mean(row[arm]["elapsed_ms"] for row in rows), 2),
            "median_latency_ms": round(statistics.median(row[arm]["elapsed_ms"] for row in rows), 2),
            "total_calls": sum(row[arm]["usage"]["logical_invocations"] for row in rows),
            "input_tokens": sum(row[arm]["usage"]["input_tokens"] for row in rows),
            "output_tokens": sum(row[arm]["usage"]["output_tokens"] for row in rows),
            "product_judge_calls": sum(row[arm]["product_judge_calls"] for row in rows),
            "tool_calls": sum(row[arm]["tool_calls"] for row in rows),
            "agent_failures": sum(row[arm]["agent_partial_or_failed"] for row in rows),
            "task_clean_samples": sum(not row[arm]["task_failures"] for row in rows),
            "tool_attempts": attempts,
            "successful_tools": successful,
            "tool_effectiveness": round(successful / attempts, 3) if attempts else None,
            "repeat_rate": round(repeats / attempts, 3) if attempts else None,
            "exact_location_recall": round(
                sum(item["exact_location_hits_ignoring_category"] for item in diagnostics) / expected_total,
                3,
            ),
            "within_5_lines_recall": round(
                sum(item["within_5_lines_hits_ignoring_category"] for item in diagnostics) / expected_total,
                3,
            ),
            "negative_false_positive_samples": [
                row["id"] for row in rows
                if row[arm]["expected_count"] == 0 and row[arm]["semantic_judges"][0]["reported"] > 0
            ],
            "missed_positive_samples": [
                row["id"] for row in rows
                if row[arm]["expected_count"] > 0 and row[arm]["semantic_judges"][0]["matched"] == 0
            ],
        }
    report["delta_v3_minus_base"] = {
        key: round(report["arms"]["v3"]["judge_1"][key] - report["arms"]["base"]["judge_1"][key], 3)
        for key in ("precision", "recall", "f1")
    }
    return report


def _markdown(report: dict[str, Any]) -> str:
    base = report["arms"]["base"]
    v3 = report["arms"]["v3"]
    sample_count = report["sample_count"]
    def ratio(numerator: int, denominator: int) -> str:
        return f"{numerator / denominator:.2f}" if denominator else "n/a"
    lines = [f"# V3 真实模型 {sample_count} 题：Base 对照", "", f"> 主模型 `{report['model']}`；两个独立语义 Judge 均为 `{report['judge_model']}`。Pipeline 产品 Judge 已关闭，避免它在看不到标准答案时提前过滤候选。", "",
             f"> 数据集为固定代码快照的跨文件上下文评测，本报告包含 {sample_count} 题；本轮不是线上真实 PR 外部验证集。", "",
             "| 指标 | Base | V3 |", "|---|---:|---:|"]
    for label, judge, key in (("Judge 1 Precision", "judge_1", "precision"), ("Judge 1 Recall", "judge_1", "recall"), ("Judge 1 F1", "judge_1", "f1"), ("Judge 2 Precision", "judge_2", "precision"), ("Judge 2 Recall", "judge_2", "recall"), ("Judge 2 F1", "judge_2", "f1"), ("Judge 1 完全正确样本", "judge_1", "perfect_samples"), ("Judge 2 完全正确样本", "judge_2", "perfect_samples")):
        lines.append(f"| {label} | {report['arms']['base'][judge][key]} | {report['arms']['v3'][judge][key]} |")
    lines += [f"| 双 Judge 一致样本 | {report['arms']['base']['judge_agreement_samples']}/{sample_count} | {report['arms']['v3']['judge_agreement_samples']}/{sample_count} |",
              f"| 平均延迟 ms | {report['arms']['base']['avg_latency_ms']} | {report['arms']['v3']['avg_latency_ms']} |",
              f"| 中位延迟 ms | {report['arms']['base']['median_latency_ms']} | {report['arms']['v3']['median_latency_ms']} |",
              f"| 精确行定位召回 | {report['arms']['base']['exact_location_recall']} | {report['arms']['v3']['exact_location_recall']} |",
              f"| ±5 行定位召回 | {report['arms']['base']['within_5_lines_recall']} | {report['arms']['v3']['within_5_lines_recall']} |",
              f"| 任务正常完成样本 | {report['arms']['base']['task_clean_samples']}/{sample_count} | {report['arms']['v3']['task_clean_samples']}/{sample_count} |",
              f"| 主链路模型调用 | {report['arms']['base']['total_calls']} | {report['arms']['v3']['total_calls']} |",
              f"| 输入 token | {base['input_tokens']} | {v3['input_tokens']} |",
              f"| 输出 token | {base['output_tokens']} | {v3['output_tokens']} |",
              f"| 工具调用 | {report['arms']['base']['tool_calls']} | {report['arms']['v3']['tool_calls']} |",
              f"| 工具有效率 | n/a | {report['arms']['v3']['tool_effectiveness']} |",
              f"| 重复调用率 | n/a | {report['arms']['v3']['repeat_rate']} |",
              "", f"> V3-Base: Precision {report['delta_v3_minus_base']['precision']:+}，Recall {report['delta_v3_minus_base']['recall']:+}，F1 {report['delta_v3_minus_base']['f1']:+}。",
             "", "## 严格错误明细", "",
             f"- Base 负样本误报：{', '.join(base['negative_false_positive_samples']) or '无'}",
             f"- Base 正样本漏检：{', '.join(base['missed_positive_samples']) or '无'}",
             f"- V3 负样本误报：{', '.join(v3['negative_false_positive_samples']) or '无'}",
             f"- V3 正样本漏检：{', '.join(v3['missed_positive_samples']) or '无'}",
             "", f"V3 本轮漏检 {len(v3['missed_positive_samples'])} 个正样本，负样本误报 {len(v3['negative_false_positive_samples'])} 个；样本 ID 以上述严格错误明细为准。",
             "", "## 开销与口径", "",
             f"V3 主链路调用量是 Base 的 {ratio(v3['total_calls'], base['total_calls'])} 倍，输入 token 是 {ratio(v3['input_tokens'], base['input_tokens'])} 倍，输出 token 是 {ratio(v3['output_tokens'], base['output_tokens'])} 倍。未配置模型单价，因此报告 token 用量，不虚构美元成本。",
             "", "合并报告使用同一配置下失败样本的定向重跑结果；延迟受重试批次和模型波动影响，适合看量级，不适合作为严格的配对性能结论。",
             "", "> Pipeline 只执行结构、位置、证据和去重校验；最终语义由两个能看到标准答案的 Judge 独立裁决。",
             f"> execution_completed={str(report['execution_completed']).lower()}"]
    return "\n".join(lines)


async def _main(args: argparse.Namespace) -> int:
    base = Settings()
    key, url = resolve_credentials(base.llm)
    if not key or not url:
        print("缺少主模型 API 配置")
        return 1
    cfg = SemanticJudgeConfig.load(Path(args.judge_config))
    model = args.model or base.llm.model
    ds = EvalDataset.load_yaml(Path(args.dataset))
    samples = ds.samples
    if args.sample_ids:
        wanted = set(args.sample_ids.split(","))
        samples = [s for s in samples if s.id in wanted]
    checkpoint = Path(args.checkpoint)
    rows: list[dict[str, Any]] = []
    if checkpoint.exists() and not args.fresh:
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        if payload.get("model") == model and payload.get("sample_ids") == [s.id for s in samples]:
            rows = payload["rows"]
    done = {row["id"] for row in rows}
    provider = OpenAICompatProvider.from_config(_settings(model, agentic=False).llm)
    judges = [OpenAICompatProvider(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
        temperature=cfg.temperature, timeout_seconds=cfg.timeout_seconds, max_retries=cfg.max_retries,
        max_output_tokens=cfg.max_output_tokens) for _ in range(2)]
    try:
        for index, sample in enumerate(samples, 1):
            if sample.id in done:
                continue
            print(f"[{index}/{len(samples)}] {sample.id}", flush=True)
            try:
                row = await _sample(sample, provider, judges, cfg, model)
            except Exception as exc:
                row = {"id": sample.id, "kind": sample.kind, "fatal_error": {"kind": type(exc).__name__, "detail": _redact(str(exc))}}
            rows.append(row)
            checkpoint.write_text(json.dumps({"model": model, "sample_ids": [s.id for s in samples], "rows": rows}, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    finally:
        await provider.aclose()
        for judge in judges:
            await judge.aclose()
    complete = [row for row in rows if "base" in row and "v3" in row]
    report = _aggregate(complete, model, cfg.model)
    report["failures"] = [row for row in rows if "fatal_error" in row]
    report["execution_completed"] = (
        len(complete) == len(samples)
        and all(not row["base"]["task_failures"] and not row["v3"]["task_failures"] for row in complete)
    )
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    Path(args.markdown).write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report["arms"], ensure_ascii=False, indent=2))
    return 0 if report["execution_completed"] else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=DATASET)
    parser.add_argument("--output", default=OUTPUT)
    parser.add_argument("--markdown", default=MARKDOWN)
    parser.add_argument("--checkpoint", default=CHECKPOINT)
    parser.add_argument("--judge-config", default="config/semantic_judge.local.json")
    parser.add_argument("--model", default="")
    parser.add_argument("--sample-ids", default="")
    parser.add_argument("--fresh", action="store_true")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
