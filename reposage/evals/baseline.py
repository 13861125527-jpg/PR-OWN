"""V1 vs 直拼 Prompt baseline 对照（evals/baseline.py，11 §6 / V1-d DoD）。

> **命名（P1-3 返工）**：本文件是 **Pipeline 确定性回归**（scripted candidates →
> 同一份输入跑两种后处理：V1 完整链路 vs 直拼 Prompt），用于验证 Pipeline 的
> 确定性收益（重定位修正行号、去重降噪、幻觉路径抑制），**不代表真实模型质量
> 验收**。真实模型 V1 vs baseline 对照（两边各跑 Prompt/Provider 链路）需配置
> key 后执行，见 docs/evidence 的 V1-D 状态说明；未执行前 V1-D 真实质量 DoD
> 视为"未完成"。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from reposage.domain.enums import FindingStatus
from reposage.domain.finding import Finding, FindingCandidate
from reposage.domain.models import ChangedFile
from reposage.evals.dataset import EvalDataset, EvalSample
from reposage.evals.metrics import Metrics, compute_metrics
from reposage.evals.scripted import scripted_candidates
from reposage.review.pipeline import FindingPipeline


def baseline_findings(candidates: list[FindingCandidate], sample: EvalSample) -> list[Finding]:
    """直拼 Prompt baseline：候选 claimed 位置直接作为 Finding（无重定位/去重）。"""
    out: list[Finding] = []
    for cand in candidates:
        out.append(
            Finding(
                finding_occurrence_id=f"base-{len(out)}",
                run_id=sample.id,
                fingerprint="",
                cross_run_match_key="",
                title=cand.title,
                severity=cand.severity,
                confidence=cand.confidence,
                category=cand.category,
                claimed_path=cand.claimed_path,
                claimed_start_line=cand.claimed_start_line,
                canonical_path=cand.claimed_path,  # 直拼：claimed 当 canonical
                canonical_start_line=cand.claimed_start_line,
                status=FindingStatus.ACCEPTED,  # 无门槛
            )
        )
    return out


def _evaluate(findings: list[Finding], sample: EvalSample) -> Metrics:
    m = compute_metrics(sample.expected, findings, sample_kind=sample.kind)
    m.details["sample_kind"] = sample.kind
    m.details["n_expected"] = len(sample.expected)
    return m


def compare(
    dataset_path: str | Path,
    *,
    scripted: Callable[[str], list[FindingCandidate]] = scripted_candidates,
    repo: str = "eval-repo",
) -> dict[str, dict[str, object]]:
    """输出三张对照表：质量 / 成本 / 延迟（macro 聚合）。"""
    ds = EvalDataset.load_yaml(Path(dataset_path))
    pipeline = FindingPipeline(repo=repo, head_sha="head", min_confidence=0.0)

    v1_metrics: list[Metrics] = []
    base_metrics: list[Metrics] = []
    v1_latency_ms = 0.0
    base_latency_ms = 0.0

    for sample in ds.samples:
        cands = scripted(sample.id)
        # V1：完整链路（重定位/去重/门槛）
        t0 = time.perf_counter()
        v1_findings = pipeline.process(
            run_id=f"eval-{sample.id}", candidates=cands, file_map=_file_map_of(sample)
        )
        v1_latency_ms += (time.perf_counter() - t0) * 1000
        v1_metrics.append(_evaluate(v1_findings, sample))
        # baseline：直拼 Prompt
        t0 = time.perf_counter()
        base_findings = baseline_findings(cands, sample)
        base_latency_ms += (time.perf_counter() - t0) * 1000
        base_metrics.append(_evaluate(base_findings, sample))

    def _avg(ms: list[Metrics], attr: str) -> float:
        return sum(getattr(m, attr) for m in ms) / len(ms) if ms else 0.0

    def _avg_position(ms: list[Metrics]) -> float:
        """位置准确率只对有 expected 的样本聚合（无位置要求者不参与，P1-2）。"""
        pos = [m.position_accuracy for m in ms if m.details.get("n_expected", 0) > 0]
        return sum(pos) / len(pos) if pos else 0.0

    quality: dict[str, object] = {
        "precision": {"v1": round(_avg(v1_metrics, "precision"), 3), "baseline": round(_avg(base_metrics, "precision"), 3)},
        "recall": {"v1": round(_avg(v1_metrics, "recall"), 3), "baseline": round(_avg(base_metrics, "recall"), 3)},
        "position_accuracy": {"v1": round(_avg_position(v1_metrics), 3), "baseline": round(_avg_position(base_metrics), 3)},
        "negative_noise": {"v1": float(sum(m.negative_noise for m in v1_metrics)), "baseline": float(sum(m.negative_noise for m in base_metrics))},
    }
    # 成本（P1-3）：脚本化评测未调用真实模型、未定价 → 标记 unknown，不伪造 0 成本
    cost: dict[str, object] = {
        "model_calls": {"v1": 0, "baseline": 0},
        "cost_usd": {"v1": "unknown", "baseline": "unknown"},
        "note": "Pipeline 确定性回归未调用真实模型；费用未定价标记 unknown，真实对照见 docs/evidence V1-D 状态说明",
    }
    latency: dict[str, object] = {
        "total_ms": {"v1": round(v1_latency_ms, 2), "baseline": round(base_latency_ms, 2)},
        "note": "脚本化下仅 pipeline 计算耗时；真实模型延迟对照待 key",
    }
    result: dict[str, dict[str, object]] = {"quality": quality, "cost": cost, "latency": latency}
    return result


def _file_map_of(sample: EvalSample) -> dict[str, ChangedFile]:
    """样本文件路径映射（pipeline 需要 ChangedFile）。"""
    import asyncio

    from reposage.domain.diff import filter_files, parse_unified_diff
    from reposage.providers.git.fake import FakeGitProvider

    fake = FakeGitProvider()
    fake.add_snapshot("base", sample.base_files)
    fake.add_snapshot("head", sample.head_files)
    diff = asyncio.run(fake.get_diff("base", "head"))
    files = parse_unified_diff(diff)
    filtered = filter_files(files, languages=["python"], max_files=40)
    return {f.path: f for f in filtered.kept}


def render_tables(result: dict[str, dict[str, object]]) -> str:
    lines = ["# Pipeline 确定性回归：V1 vs 直拼 Prompt baseline（脚本化，非真实模型对照）", ""]
    lines.append("## 质量（macro 平均）")
    lines.append("| 指标 | V1 | baseline |")
    lines.append("|------|----|----------|")
    quality = result["quality"]
    if isinstance(quality, dict):
        for k, pair in quality.items():
            if isinstance(pair, dict):
                lines.append(f"| {k} | {pair['v1']} | {pair['baseline']} |")
    lines.append("")
    lines.append("## 成本")
    lines.append("| 项 | V1 | baseline |")
    lines.append("|----|----|----------|")
    cost = result["cost"]
    if isinstance(cost, dict):
        for k, pair in cost.items():
            if isinstance(pair, dict):
                lines.append(f"| {k} | {pair['v1']} | {pair['baseline']} |")
    lines.append("")
    lines.append("## 延迟")
    lines.append("| 项 | V1 | baseline |")
    lines.append("|----|----|----------|")
    latency = result["latency"]
    if isinstance(latency, dict):
        for k, pair in latency.items():
            if isinstance(pair, dict):
                lines.append(f"| {k} | {pair['v1']} | {pair['baseline']} |")
    lines.append("")
    lines.append("> 说明：本对照为 Pipeline 确定性回归（同一份脚本化候选跑两种后处理），")
    lines.append("> 只证明 Pipeline 重定位/去重/抑制的确定性收益，不构成真实模型质量验收。")
    for section, label in ((cost, "成本"), (latency, "延迟")):
        if isinstance(section, dict):
            note = section.get("note")
            if note:
                lines.append(f"> {label}：{note}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "reposage/evals/datasets/v1_demo.yaml"
    result = compare(path)
    print(render_tables(result))
