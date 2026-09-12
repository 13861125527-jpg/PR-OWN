"""Run the real V2 (multi-role + L3 context) evaluation with checkpoints."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from reposage.config.settings import RoleOverlayConfig, Settings
from reposage.domain.diff import filter_files, parse_unified_diff
from reposage.domain.enums import (
    ChangeRequestSource,
    ContextLayer,
    FindingStatus,
    ReviewRunStatus,
    ReviewStrategyName,
)
from reposage.domain.models import ChangeRequest, CommitRef, GlobalBudget
from reposage.domain.run import ReviewRun
from reposage.evals.dataset import EvalDataset, EvalSample
from reposage.evals.metrics import Metrics, compute_metrics
from reposage.evals.semantic_judge import (
    SemanticJudgeConfig,
    compute_semantic_metrics,
    finding_summary,
)
from reposage.providers.git.fake import FakeGitProvider
from reposage.providers.llm.openai_compat import OpenAICompatProvider, resolve_credentials
from reposage.review.adjudicator import LlmAdjudicator
from reposage.review.context import ContextAssembler
from reposage.review.pipeline import FindingPipeline
from reposage.review.reviewers.roles.multi_role import MultiRoleReviewer
from reposage.review.reviewers.roles.registry import RoleRegistry
from reposage.review.symbols.snapshot import prepare_l3_snapshot

DATASET = "reposage/evals/datasets/resume_v123_40.yaml"
OUTPUT = "docs/evidence/resume-v2-broad-gate-retest.json"
MARKDOWN = "docs/evidence/resume-v2-broad-gate-retest.md"
CHECKPOINT = "docs/evidence/resume-v2-broad-gate-retest.checkpoint.json"
REFERENCE_REPORT = "docs/evidence/resume-v2-real.json"


def _redact(text: str) -> str:
    text = re.sub(r"(?i)(sk-[a-z0-9_-]{4,}|bearer\s+[a-z0-9._-]{4,})", "<redacted>", text)
    return text[:200]


def _json_default(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _cat(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _metrics_dict(value: Metrics) -> dict[str, Any]:
    return {
        "precision": value.precision,
        "recall": value.recall,
        "f1": value.f1,
        "position_accuracy": value.position_accuracy,
        "negative_noise": value.negative_noise,
    }


def _diagnostics(sample: EvalSample, findings: list[Any]) -> dict[str, Any]:
    expected = [{"category": e.category, "path": e.path, "line": e.line} for e in sample.expected]
    actual = [
        {
            "category": _cat(f.category),
            "path": f.canonical_path,
            "line": f.canonical_start_line,
            "confidence": f.confidence,
        }
        for f in findings
    ]
    used: set[int] = set()
    strict = exact_location = within5 = path_only = 0
    confusions: list[dict[str, Any]] = []
    for exp in sample.expected:
        candidates = [(i, f) for i, f in enumerate(findings) if i not in used and f.canonical_path == exp.path]
        if not candidates:
            continue
        path_only += 1
        if exp.line is None:
            near = candidates
        else:
            near = [(i, f) for i, f in candidates if f.canonical_start_line is not None and abs(f.canonical_start_line - exp.line) <= 5]
        if near:
            within5 += 1
        exact = [(i, f) for i, f in candidates if f.canonical_start_line == exp.line]
        if exact:
            exact_location += 1
        same = [(i, f) for i, f in exact if _cat(f.category) == exp.category]
        chosen = (same or exact or near or candidates)[0]
        used.add(chosen[0])
        if same:
            strict += 1
        elif exact:
            confusions.append({
                "path": exp.path,
                "line": exp.line,
                "expected": exp.category,
                "actual": _cat(chosen[1].category),
            })
    return {
        "expected": expected,
        "accepted": actual,
        "strict_hits": strict,
        "exact_location_hits_ignoring_category": exact_location,
        "within_5_lines_hits_ignoring_category": within5,
        "path_hits_ignoring_category": path_only,
        "category_confusions_at_exact_location": confusions,
    }


def _settings(
    model: str, *, symbol_retrieval: bool = True, judge_enabled: bool = False
) -> Settings:
    base = Settings()
    return base.model_copy(
        update={
            "llm": base.llm.model_copy(update={"model": model}),
            "review": base.review.model_copy(
                update={
                    "strategy": "multi_role",
                    "roles": ["general", "security", "correctness", "performance"],
                    "role_overrides": {
                        "correctness": RoleOverlayConfig(gate_id="added_lines")
                    },
                    "static": base.review.static.model_copy(update={"enabled": False}),
                    "judge": base.review.judge.model_copy(
                        update={
                            "enabled": judge_enabled,
                            "max_output_tokens": 3000,
                            "timeout_seconds": 90,
                        }
                    ),
                    "feedback": base.review.feedback.model_copy(update={"enabled": False}),
                    "incremental": base.review.incremental.model_copy(update={"enabled": False}),
                }
            ),
            "context": base.context.model_copy(update={"symbol_retrieval": symbol_retrieval}),
            "agent": base.agent.model_copy(update={"enabled": False}),
        }
    )


async def _run_sample(
    sample: EvalSample,
    llm: OpenAICompatProvider,
    settings: Settings,
    *,
    semantic_llm: OpenAICompatProvider | None = None,
    semantic_config: SemanticJudgeConfig | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    fake = FakeGitProvider(repository_id=f"eval-{sample.id}")
    fake.add_snapshot("base", sample.base_files)
    fake.add_snapshot("head", sample.head_files)
    request = ChangeRequest(
        source=ChangeRequestSource.LOCAL_RANGE,
        base=CommitRef(sha="base", label="base"),
        head=CommitRef(sha="head", label="head", locked=True),
        title=sample.pr_title or None,
        description=sample.pr_description or None,
    )
    diff = await fake.get_diff("base", "head")
    filtered = filter_files(parse_unified_diff(diff), languages=settings.review.languages, max_files=40)
    file_map = {f.path: f for f in filtered.kept}
    prepared = None
    if settings.context.symbol_retrieval:
        prepared = await prepare_l3_snapshot(
            fake, "head", filtered.kept, repository_id=fake.repository_id
        )
    assembler = ContextAssembler.from_settings(settings)
    run = ReviewRun(
        run_id=f"v2-{sample.id}", strategy=ReviewStrategyName.MULTI_ROLE, status=ReviewRunStatus.RUNNING
    )
    units = []
    for changed in filtered.kept:
        units.extend(assembler.build_file_units(
            run_id=run.run_id,
            change_request=request,
            file=changed,
            snapshot=prepared.snapshot if prepared is not None else None,
        ))
    l3_hits = sum(
        1
        for unit in units
        for chunk in unit.context.chunks
        if chunk.layer is ContextLayer.L3
    )
    budget = GlobalBudget(
        max_total_tokens=settings.budget.max_total_tokens,
        max_cost_usd=settings.budget.max_cost_usd,
        max_runtime_seconds=settings.budget.max_runtime_seconds,
    )
    reviewer = MultiRoleReviewer(
        llm,
        RoleRegistry(settings),
        languages=settings.review.languages,
        file_tasks=settings.concurrency.file_tasks,
        role_tasks=settings.concurrency.role_tasks,
        model_requests=settings.concurrency.model_requests,
        input_price_per_1k=settings.llm.input_price_per_1k,
        output_price_per_1k=settings.llm.output_price_per_1k,
    )
    result = await reviewer.execute(units, run, budget)
    adjudicator = None
    model_semaphore = None
    if settings.review.judge.enabled:
        adjudicator = LlmAdjudicator(
            llm,
            settings.review.judge,
            model=settings.llm.model,
            input_price_per_1k=settings.llm.input_price_per_1k,
            output_price_per_1k=settings.llm.output_price_per_1k,
        )
        model_semaphore = asyncio.Semaphore(settings.concurrency.model_requests)
    processed = await FindingPipeline(
        repo="eval-repo",
        head_sha="head",
        min_confidence=0.0,
        judge_enabled=settings.review.judge.enabled,
        max_findings=settings.review.judge.max_findings,
    ).process(
        run_id=run.run_id,
        candidates=result.candidates,
        file_map=file_map,
        adjudicator=adjudicator,
        budget=budget if adjudicator is not None else None,
        model_semaphore=model_semaphore,
    )
    accepted = [f for f in processed.findings if f.status is FindingStatus.ACCEPTED]
    metrics = compute_metrics(sample.expected, accepted, sample_kind=sample.kind)
    semantic_metrics = None
    semantic_error = None
    if semantic_llm is not None and semantic_config is not None:
        try:
            semantic_metrics = await compute_semantic_metrics(
                sample.expected,
                accepted,
                semantic_llm,
                line_tolerance=5,
                temperature=semantic_config.temperature,
                max_tokens=semantic_config.max_output_tokens,
            )
        except Exception as exc:
            semantic_error = {"kind": type(exc).__name__, "detail": _redact(str(exc))}
    usages = [*result.source_run.usages, *processed.usages]
    tasks = [*result.source_run.tasks, *processed.tasks]
    gates = result.source_run.gate_decisions
    elapsed = (time.perf_counter() - started) * 1000
    return {
        "id": sample.id,
        "kind": sample.kind,
        "metrics": _metrics_dict(metrics),
        "elapsed_ms": round(elapsed, 2),
        "expected_count": len(sample.expected),
        "accepted_count": len(accepted),
        "candidate_count": len(result.candidates),
        "l3_hits": l3_hits,
        "l3_diagnostics": asdict(prepared.diagnostics) if prepared is not None else None,
        "gate_decisions": [g.model_dump(mode="json") for g in gates],
        "tasks": [t.model_dump(mode="json") for t in tasks],
        "health": result.source_run.health.model_dump(mode="json"),
        "usage": {
            "logical_invocations": len(usages),
            "provider_requests": sum(1 + u.retries + u.schema_repairs for u in usages),
            "input_tokens": sum(u.input_tokens for u in usages),
            "output_tokens": sum(u.output_tokens for u in usages),
            "retries": sum(u.retries for u in usages),
            "schema_repairs": sum(u.schema_repairs for u in usages),
            "provider_latency_ms": sum(u.latency_ms for u in usages),
            "cost_usd": round(sum(u.cost_usd for u in usages), 6),
            "by_role": {
                role: sum(1 for u in usages if u.role == role)
                for role in sorted({u.role for u in usages})
            },
        },
        "diagnostics": _diagnostics(sample, accepted),
        "finding_details": [finding_summary(f) for f in accepted],
        "semantic_metrics": (
            semantic_metrics.model_dump(mode="json") if semantic_metrics is not None else None
        ),
        "semantic_error": semantic_error,
        "warnings": [
            _redact(w) for w in [*result.source_run.warnings, *processed.warnings]
        ],
        "failures": [
            {"target": t.target, "detail": _redact(t.error or "unknown")}
            for t in tasks if t.status.value == "failed"
        ],
    }


def _save_checkpoint(path: Path, rows: list[dict[str, Any]], *, dataset: str, model: str, sample_ids: list[str], symbol_retrieval: bool) -> None:
    payload = {"dataset": dataset, "model": model, "sample_ids": sample_ids, "symbol_retrieval": symbol_retrieval, "completed_sample_ids": [r["id"] for r in rows], "results": rows}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    tmp.replace(path)


def _load_checkpoint(path: Path, *, dataset: str, model: str, sample_ids: list[str], symbol_retrieval: bool) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("dataset") != dataset or raw.get("model") != model or raw.get("sample_ids") != sample_ids or raw.get("symbol_retrieval") != symbol_retrieval:
        raise ValueError("检查点的数据集或模型不匹配")
    results = raw.get("results", [])
    if not isinstance(results, list):
        raise ValueError("检查点 results 必须是列表")
    return [item for item in results if isinstance(item, dict)]


def _aggregate(rows: list[dict[str, Any]], dataset: str, model: str, reference_path: Path, expected_samples: int, symbol_retrieval: bool) -> dict[str, Any]:
    complete = [r for r in rows if not r.get("fatal_error")]
    metrics = [r["metrics"] for r in complete]
    positive = [r for r in complete if r["expected_count"]]
    total_expected = sum(r["expected_count"] for r in complete)
    diag = [r["diagnostics"] for r in complete]
    strict = sum(d["strict_hits"] for d in diag)
    exact_loc = sum(d["exact_location_hits_ignoring_category"] for d in diag)
    within5 = sum(d["within_5_lines_hits_ignoring_category"] for d in diag)
    def avg(key: str, values: list[dict[str, Any]] = metrics) -> float:
        return round(sum(v[key] for v in values) / len(values), 3) if values else 0.0
    summary = {
        "precision_macro": avg("precision"),
        "recall_macro": avg("recall"),
        "f1_macro": avg("f1"),
        "position_accuracy_macro_positive": round(sum(r["metrics"]["position_accuracy"] for r in positive) / len(positive), 3) if positive else 0.0,
        "negative_noise": sum(r["metrics"]["negative_noise"] for r in complete),
        "strict_defect_recall": round(strict / total_expected, 3) if total_expected else 1.0,
        "exact_location_recall_ignoring_category": round(exact_loc / total_expected, 3) if total_expected else 1.0,
        "within_5_lines_recall_ignoring_category": round(within5 / total_expected, 3) if total_expected else 1.0,
        "perfect_samples": sum(1 for r in complete if r["metrics"]["f1"] == 1.0),
    }
    semantic_rows = [r["semantic_metrics"] for r in complete if r.get("semantic_metrics")]
    if semantic_rows:
        semantic_matched = sum(r["matched"] for r in semantic_rows)
        semantic_expected = sum(r["expected"] for r in semantic_rows)
        semantic_reported = sum(r["reported"] for r in semantic_rows)
        semantic_raw_reported = sum(r.get("raw_reported", r["reported"]) for r in semantic_rows)
        semantic_duplicates = sum(r.get("duplicates_suppressed", 0) for r in semantic_rows)
        semantic_precision = (
            semantic_matched / semantic_reported
            if semantic_reported
            else (1.0 if not semantic_expected else 0.0)
        )
        semantic_recall = semantic_matched / semantic_expected if semantic_expected else 1.0
        semantic_f1 = (
            2 * semantic_precision * semantic_recall / (semantic_precision + semantic_recall)
            if semantic_precision + semantic_recall
            else 0.0
        )
        category_matches = sum(r["category_agreement_count"] for r in semantic_rows)
        summary["semantic_judge"] = {
            "samples": len(semantic_rows),
            "matched": semantic_matched,
            "expected": semantic_expected,
            "reported": semantic_reported,
            "raw_reported": semantic_raw_reported,
            "duplicates_suppressed": semantic_duplicates,
            "precision_micro": round(semantic_precision, 3),
            "recall_micro": round(semantic_recall, 3),
            "f1_micro": round(semantic_f1, 3),
            "perfect_samples": sum(
                r["precision"] == 1.0 and r["recall"] == 1.0 for r in semantic_rows
            ),
            "category_agreement": round(
                category_matches / semantic_matched, 3
            ) if semantic_matched else 0.0,
            "line_tolerance": 5,
        }
    reference = None
    if reference_path.exists():
        old = json.loads(reference_path.read_text(encoding="utf-8"))
        selected = {r["id"] for r in complete}
        old_rows = [r for r in old.get("per_sample", []) if r.get("id") in selected]
        if complete and len(old_rows) == len(complete):
            old_metrics = [r["metrics"] for r in old_rows]
            old_positive = [r for r in old_rows if r["expected_count"]]
            old_expected = sum(r["expected_count"] for r in old_rows)
            old_strict = sum(r["diagnostics"]["strict_hits"] for r in old_rows)
            old_exact = sum(r["diagnostics"]["exact_location_hits_ignoring_category"] for r in old_rows)
            old_within5 = sum(r["diagnostics"]["within_5_lines_hits_ignoring_category"] for r in old_rows)
            reference = {
                "precision_macro": avg("precision", old_metrics),
                "recall_macro": avg("recall", old_metrics),
                "f1_macro": avg("f1", old_metrics),
                "position_accuracy_macro_positive": round(sum(r["metrics"]["position_accuracy"] for r in old_positive) / len(old_positive), 3),
                "negative_noise": sum(r["metrics"]["negative_noise"] for r in old_rows),
                "strict_defect_recall": round(old_strict / old_expected, 3),
                "exact_location_recall_ignoring_category": round(old_exact / old_expected, 3),
                "within_5_lines_recall_ignoring_category": round(old_within5 / old_expected, 3),
            }
    elapsed = [r["elapsed_ms"] for r in complete]
    usage_keys = ("logical_invocations", "provider_requests", "input_tokens", "output_tokens", "retries", "schema_repairs", "provider_latency_ms", "cost_usd")
    report = {
        "version": "V2",
        "configuration": {
            "strategy": "multi_role", "roles": ["general", "security", "correctness", "performance"],
            "symbol_retrieval": symbol_retrieval,
            "static": False,
            "judge": any(
                task.get("kind") == "judge_adjudicate"
                for row in complete
                for task in row.get("tasks", [])
            ),
            "feedback": False,
            "semantic_judge": bool(semantic_rows),
            "pipeline_min_confidence": 0.0, "correctness_gate": "added_lines",
        },
        "model": model, "dataset": dataset, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "sample_count": len(rows), "successful_samples": len(complete),
        "execution_completed": (
            len(rows) == expected_samples
            and len(complete) == len(rows)
            and not any(r.get("failures") or r.get("semantic_error") for r in complete)
        ),
        "summary": summary,
        "original_v2_same_samples": reference,
        "delta_vs_original_v2": None if not reference else {
            key: round(summary[key] - reference[key], 3)
            for key in ("precision_macro", "recall_macro", "f1_macro", "position_accuracy_macro_positive", "negative_noise", "strict_defect_recall", "exact_location_recall_ignoring_category", "within_5_lines_recall_ignoring_category")
        },
        "latency": {
            "total_ms": round(sum(elapsed), 2), "avg_ms": round(statistics.mean(elapsed), 2) if elapsed else 0,
            "median_ms": round(statistics.median(elapsed), 2) if elapsed else 0,
        },
        "usage": {k: round(sum(r["usage"][k] for r in complete), 6) for k in usage_keys},
        "roles": {role: sum(r["usage"]["by_role"].get(role, 0) for r in complete) for role in ("general", "security", "correctness", "performance")},
        "l3": {"total_hits": sum(r["l3_hits"] for r in complete), "samples_with_hits": sum(bool(r["l3_hits"]) for r in complete)},
        "category_confusions_at_exact_location": [dict(item, sample_id=r["id"]) for r in complete for item in r["diagnostics"]["category_confusions_at_exact_location"]],
        "per_sample": rows,
    }
    return report


def _markdown(report: dict[str, Any]) -> str:
    s, u, lat = report["summary"], report["usage"], report["latency"]
    lines = [
        "# RepoSage V2 宽门控真实模型复测", "",
        f"> `{report['model']}`；MultiRole + L3；{report['sample_count']} 题；correctness=added_lines；Pipeline 阈值 0.0。", "",
        "## 结果", "", "| 指标 | 宽门控 V2 | 相对原 V2 同题 |", "|---|---:|---:|",
    ]
    delta = report.get("delta_vs_original_v2") or {}
    for label, key in (("Precision (macro)", "precision_macro"), ("Recall (macro)", "recall_macro"), ("F1 (macro)", "f1_macro"), ("位置准确率（正样本 macro）", "position_accuracy_macro_positive"), ("负样本噪声", "negative_noise")):
        lines.append(f"| {label} | {s[key]} | {delta.get(key, 'n/a')} |")
    lines += ["", "## 更可解释的缺陷级指标", "", "| 指标 | V2 |", "|---|---:|",
              f"| category + path + exact line | {s['strict_defect_recall']} |",
              f"| path + exact line（忽略 category） | {s['exact_location_recall_ignoring_category']} |",
              f"| path + ±5 lines（忽略 category） | {s['within_5_lines_recall_ignoring_category']} |",
              ]
    if "semantic_judge" in s:
        sj = s["semantic_judge"]
        lines += [
            f"| V4 FLASH 语义 Precision（micro） | {sj['precision_micro']} |",
            f"| V4 FLASH 语义 Recall（micro） | {sj['recall_micro']} |",
            f"| V4 FLASH 语义 F1（micro） | {sj['f1_micro']} |",
            f"| 类别一致率（仅诊断） | {sj['category_agreement']} |",
        ]
    lines += ["", "## 调用与延迟", "", "| 项 | 数值 |", "|---|---:|",
              f"| 成功样本 | {report['successful_samples']}/{report['sample_count']} |",
              f"| 逻辑模型调用 | {u['logical_invocations']} |", f"| Provider 请求 | {u['provider_requests']} |",
              f"| 输入 Token | {u['input_tokens']} |", f"| 输出 Token | {u['output_tokens']} |",
              f"| 平均端到端延迟 | {lat['avg_ms']} ms |", f"| 中位延迟 | {lat['median_ms']} ms |",
              f"| L3 命中样本 | {report['l3']['samples_with_hits']}/{report['successful_samples']} |", "",
              f"> execution_completed={str(report['execution_completed']).lower()}"]
    return "\n".join(lines)


async def _main(args: argparse.Namespace) -> int:
    base = Settings()
    api_key, base_url = resolve_credentials(base.llm)
    if not api_key or not base_url:
        print("缺少模型 API 配置")
        return 1
    model = args.model or os.environ.get("MODEL_NAME") or base.llm.model
    symbol_retrieval = not args.disable_l3
    settings = _settings(
        model,
        symbol_retrieval=symbol_retrieval,
        judge_enabled=args.enable_judge,
    )
    dataset = EvalDataset.load_yaml(Path(args.dataset))
    samples = dataset.samples
    if args.sample_ids:
        wanted = {item.strip() for item in args.sample_ids.split(",") if item.strip()}
        samples = [sample for sample in samples if sample.id in wanted]
        found = {sample.id for sample in samples}
        if found != wanted:
            raise ValueError(f"未找到样本: {sorted(wanted - found)}")
    selected_ids = [sample.id for sample in samples]
    checkpoint = Path(args.checkpoint)
    if args.fresh and checkpoint.exists():
        checkpoint.unlink()
    rows = _load_checkpoint(
        checkpoint,
        dataset=args.dataset,
        model=model,
        sample_ids=selected_ids,
        symbol_retrieval=symbol_retrieval,
    )
    if args.retry_failed:
        rows = [
            r for r in rows
            if not r.get("fatal_error") and not r.get("failures") and not r.get("semantic_error")
        ]
    done = {r["id"] for r in rows}
    print(f"V2 宽门控复测: model={model} samples={len(samples)} resume={len(done)}", flush=True)
    provider = OpenAICompatProvider.from_config(settings.llm)
    semantic_config = None
    semantic_provider = None
    if args.semantic_judge_config:
        semantic_config = SemanticJudgeConfig.load(Path(args.semantic_judge_config))
        semantic_provider = OpenAICompatProvider(
            model=semantic_config.model,
            api_key=semantic_config.api_key,
            base_url=semantic_config.base_url,
            temperature=semantic_config.temperature,
            timeout_seconds=semantic_config.timeout_seconds,
            max_retries=semantic_config.max_retries,
            max_output_tokens=semantic_config.max_output_tokens,
        )
    try:
        for index, sample in enumerate(samples, 1):
            if sample.id in done:
                continue
            print(f"[{index}/{len(samples)}] {sample.id}", flush=True)
            try:
                row = await _run_sample(
                    sample,
                    provider,
                    settings,
                    semantic_llm=semantic_provider,
                    semantic_config=semantic_config,
                )
            except Exception as exc:  # keep the batch resumable
                row = {"id": sample.id, "kind": sample.kind, "fatal_error": {"kind": type(exc).__name__, "detail": _redact(str(exc))}, "failures": []}
            rows.append(row)
            _save_checkpoint(
                checkpoint,
                rows,
                dataset=args.dataset,
                model=model,
                sample_ids=selected_ids,
                symbol_retrieval=symbol_retrieval,
            )
            print(f"  checkpoint {len(rows)}/{len(samples)}", flush=True)
    finally:
        await provider.aclose()
        if semantic_provider is not None:
            await semantic_provider.aclose()
    report = _aggregate(
        rows,
        args.dataset,
        model,
        Path(args.reference_report),
        len(samples),
        symbol_retrieval,
    )
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"
    )
    Path(args.markdown).write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"JSON: {args.output}\nMarkdown: {args.markdown}")
    return 0 if report["execution_completed"] else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="真实 V2 MultiRole + L3 评测")
    parser.add_argument("--dataset", default=DATASET)
    parser.add_argument("--output", default=OUTPUT)
    parser.add_argument("--markdown", default=MARKDOWN)
    parser.add_argument("--checkpoint", default=CHECKPOINT)
    parser.add_argument("--reference-report", default=REFERENCE_REPORT)
    parser.add_argument("--sample-ids", default="")
    parser.add_argument("--model", default="", help="主审查模型；默认读取 MODEL_NAME/项目配置")
    parser.add_argument("--disable-l3", action="store_true")
    parser.add_argument("--enable-judge", action="store_true")
    parser.add_argument(
        "--semantic-judge-config",
        default="",
        help="V4 FLASH 语义评测 Judge 的本地 JSON 配置",
    )
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
