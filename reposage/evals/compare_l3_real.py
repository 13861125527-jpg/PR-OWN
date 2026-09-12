"""Compare two real-evaluation reports that differ only by L3 retrieval."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _delta(on: float, off: float) -> float:
    return round(on - off, 3)


def build(off: dict[str, Any], on: dict[str, Any]) -> dict[str, Any]:
    off_rows = {row["id"]: row for row in off["per_sample"]}
    on_rows = {row["id"]: row for row in on["per_sample"]}
    if set(off_rows) != set(on_rows):
        raise ValueError("L3-off/on 样本集合不一致")
    keys = (
        "precision_macro",
        "recall_macro",
        "f1_macro",
        "position_accuracy_macro_positive",
        "negative_noise",
        "strict_defect_recall",
        "exact_location_recall_ignoring_category",
        "within_5_lines_recall_ignoring_category",
    )
    quality = {
        key: {
            "off": off["summary"][key],
            "on": on["summary"][key],
            "delta": _delta(on["summary"][key], off["summary"][key]),
        }
        for key in keys
    }
    per_sample = []
    for sample_id in off_rows:
        a, b = off_rows[sample_id], on_rows[sample_id]
        old_f1, new_f1 = a["metrics"]["f1"], b["metrics"]["f1"]
        change = "improved" if new_f1 > old_f1 else "degraded" if new_f1 < old_f1 else "equal"
        per_sample.append(
            {
                "id": sample_id,
                "kind": a["kind"],
                "off_f1": old_f1,
                "on_f1": new_f1,
                "change": change,
                "off_strict_hits": a["diagnostics"]["strict_hits"],
                "on_strict_hits": b["diagnostics"]["strict_hits"],
                "off_accepted": a["accepted_count"],
                "on_accepted": b["accepted_count"],
                "on_task_failures": len(b.get("failures", [])),
            }
        )
    return {
        "experiment": "V2 broad correctness gate: L3-off vs L3-on",
        "model": on["model"],
        "dataset": on["dataset"],
        "sample_count": len(per_sample),
        "controlled_difference": "context.symbol_retrieval false -> true",
        "execution": {
            "off_completed": off["execution_completed"],
            "on_completed": on["execution_completed"],
            "off_task_failure_samples": sum(bool(r.get("failures")) for r in off_rows.values()),
            "on_task_failure_samples": sum(bool(r.get("failures")) for r in on_rows.values()),
        },
        "retrieval": on["l3"],
        "quality": quality,
        "sample_changes": {
            "improved": sum(r["change"] == "improved" for r in per_sample),
            "degraded": sum(r["change"] == "degraded" for r in per_sample),
            "equal": sum(r["change"] == "equal" for r in per_sample),
        },
        "cost_and_latency": {
            "logical_invocations": {"off": off["usage"]["logical_invocations"], "on": on["usage"]["logical_invocations"]},
            "provider_requests": {"off": off["usage"]["provider_requests"], "on": on["usage"]["provider_requests"]},
            "input_tokens": {"off": off["usage"]["input_tokens"], "on": on["usage"]["input_tokens"], "delta": on["usage"]["input_tokens"] - off["usage"]["input_tokens"]},
            "output_tokens": {"off": off["usage"]["output_tokens"], "on": on["usage"]["output_tokens"], "delta": on["usage"]["output_tokens"] - off["usage"]["output_tokens"]},
            "avg_run_latency_ms": {"off": off["latency"]["avg_ms"], "on": on["latency"]["avg_ms"], "delta": round(on["latency"]["avg_ms"] - off["latency"]["avg_ms"], 2)},
        },
        "per_sample": per_sample,
        "limitations": [
            "L3-on 有角色任务失败的样本仍使用其他成功角色的 Finding 计算质量；执行完整性单独披露。",
            "检查点保留最终一次样本运行，重跑失败产生的历史 API 成本未计入最终 usage。",
            "单次模型抽样可能波动；严格因果结论需要多次重复运行。",
        ],
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# RepoSage L3 真实模型对照（30题）",
        "",
        f"> 模型 `{report['model']}`；唯一配置差异：`{report['controlled_difference']}`。",
        "",
        "## 质量",
        "",
        "| 指标 | L3-off | L3-on | 差值 |",
        "|---|---:|---:|---:|",
    ]
    labels = {
        "precision_macro": "Precision (macro)",
        "recall_macro": "Recall (macro)",
        "f1_macro": "F1 (macro)",
        "position_accuracy_macro_positive": "位置准确率（正样本 macro）",
        "negative_noise": "负样本误报",
        "strict_defect_recall": "严格缺陷召回",
        "exact_location_recall_ignoring_category": "精确位置召回（忽略分类）",
        "within_5_lines_recall_ignoring_category": "±5行召回（忽略分类）",
    }
    for key, label in labels.items():
        row = report["quality"][key]
        lines.append(f"| {label} | {row['off']} | {row['on']} | {row['delta']:+} |")
    e, c = report["execution"], report["cost_and_latency"]
    lines += [
        "",
        "## 执行、调用与延迟",
        "",
        "| 项 | L3-off | L3-on |",
        "|---|---:|---:|",
        f"| 完整执行 | {e['off_completed']} | {e['on_completed']} |",
        f"| 含角色失败的样本 | {e['off_task_failure_samples']} | {e['on_task_failure_samples']} |",
        f"| 逻辑调用 | {c['logical_invocations']['off']} | {c['logical_invocations']['on']} |",
        f"| Provider请求 | {c['provider_requests']['off']} | {c['provider_requests']['on']} |",
        f"| 输入Token | {c['input_tokens']['off']} | {c['input_tokens']['on']} |",
        f"| 输出Token | {c['output_tokens']['off']} | {c['output_tokens']['on']} |",
        f"| 平均端到端延迟ms | {c['avg_run_latency_ms']['off']} | {c['avg_run_latency_ms']['on']} |",
        "",
        f"> L3 实际命中 {report['retrieval']['samples_with_hits']}/{report['sample_count']}；逐题 improved={report['sample_changes']['improved']}，degraded={report['sample_changes']['degraded']}，equal={report['sample_changes']['equal']}。",
        "",
        "## 限制",
        "",
    ]
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--off", default="docs/evidence/resume-l3-off-30.json")
    parser.add_argument("--on", default="docs/evidence/resume-l3-on-30.json")
    parser.add_argument("--output", default="docs/evidence/resume-l3-real-compare-30.json")
    parser.add_argument("--markdown", default="docs/evidence/resume-l3-real-compare-30.md")
    args = parser.parse_args()
    off = json.loads(Path(args.off).read_text(encoding="utf-8"))
    on = json.loads(Path(args.on).read_text(encoding="utf-8"))
    report = build(off, on)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.markdown).write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"quality": report["quality"], "execution": report["execution"], "sample_changes": report["sample_changes"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
