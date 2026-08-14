"""真实模型 V1 vs 直拼 Prompt baseline 对照（evals/real_compare.py，V1-d P1-3）。

> **用途（V1-d 三轮验收）**：真实质量 DoD 的对照入口。两侧使用**同一个 LLM
> Provider、同一模型、同一温度、同一 max_output_tokens、同一组样本与重复次数**：
>
> - **V1 链路**：FakeGitProvider 样本快照 → diff 解析/过滤 → ContextAssembler
>   （per-file units）→ SinglePassReviewer（真实 LLM）→ FindingPipeline →
>   metrics（与 EvalRunner 一致）；
> - **直拼 Prompt baseline**：把整个 diff 原始文本拼成一条 user 消息直接交给
>   同一个 LLM（不经 V1 的上下文装配/重定位/去重），候选按 claimed 位置直接
>   匹配 → metrics。
>
> 输出脱敏 JSON + Markdown（不含 API key / Authorization / 完整请求体；
> 失败样本只记录脱敏 detail 截断）。
>
> 用法（PowerShell，key 仅本地设置）：:
>
>     $env:MODEL_BASE_URL = "https://api.deepseek.com"
>     $env:MODEL_API_KEY  = "sk-..."
>     $env:MODEL_NAME     = "deepseek-v4-pro"        # 可选覆盖
>     python -m reposage.evals.real_compare --dataset reposage/evals/datasets/v1_demo.yaml `
>         --output docs/evidence/v1-d-real-compare.json --markdown docs/evidence/v1-d-real-compare.md
>     Remove-Item Env:MODEL_API_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reposage.config.settings import Settings
from reposage.domain.diff import filter_files, parse_unified_diff
from reposage.domain.enums import (
    ChangeRequestSource,
    FindingStatus,
    ReviewStrategyName,
)
from reposage.domain.finding import Finding, FindingCandidate
from reposage.domain.models import (
    ChangeRequest,
    CommitRef,
    GlobalBudget,
    ModelUsage,
    ReviewUnit,
)
from reposage.domain.protocols import LLMProvider
from reposage.domain.run import ReviewRun
from reposage.evals.baseline import baseline_findings
from reposage.evals.dataset import EvalDataset, EvalSample
from reposage.evals.metrics import Metrics, compute_metrics
from reposage.providers.git.fake import FakeGitProvider
from reposage.providers.llm.openai_compat import (
    LLMRequestError,
    OpenAICompatProvider,
    StructuredOutputError,
)
from reposage.review.context import ContextAssembler
from reposage.review.pipeline import FindingPipeline
from reposage.review.single_pass import SinglePassReviewer, estimate_cost

_DEFAULT_DATASET = "reposage/evals/datasets/v1_demo.yaml"
_REPEATS = 1


@dataclass
class SampleResult:
    """单个样本的两侧结果（含失败与延迟）。"""

    sample_id: str
    v1_metrics: Metrics | None = None
    base_metrics: Metrics | None = None
    # usage 按每次真实 Provider 调用累计（V1 一个样本可拆多个 unit → 多次调用；
    # repeats 亦计入；不取"最后一次 usage"）
    v1_model_calls: int = 0
    base_model_calls: int = 0
    v1_input_tokens: int = 0
    v1_output_tokens: int = 0
    base_input_tokens: int = 0
    base_output_tokens: int = 0
    v1_retries: int = 0
    base_retries: int = 0
    # schema 修复重试（transport 重试之外的独立计数，V1-d 八轮 P1）
    v1_schema_repairs: int = 0
    base_schema_repairs: int = 0
    # 费用累计口径（V1-d 五轮 P1）：总量与"每次样本运行"平均分开，与 token/model_calls
    # 的累计口径一致；不再把平均费用塞进总量字段
    v1_cost_total: float = 0.0
    base_cost_total: float = 0.0
    v1_successful_runs: int = 0
    base_successful_runs: int = 0
    # 失败调用累计（V1-d 十二轮 P1）：StructuredOutputError 携带 usage，失败调用也要计入
    # 真实成本。attempted = successful + failed；报告分别列 attempted 与 successful。
    v1_failed_calls: int = 0
    base_failed_calls: int = 0
    v1_failed_input_tokens: int = 0
    v1_failed_output_tokens: int = 0
    base_failed_input_tokens: int = 0
    base_failed_output_tokens: int = 0
    v1_failed_retries: int = 0
    base_failed_retries: int = 0
    v1_failed_schema_repairs: int = 0
    base_failed_schema_repairs: int = 0
    # 失败调用成本（V1-d 十三轮 P1）：失败 usage 定价后计入 total_cost_usd。
    v1_failed_cost: float = 0.0
    base_failed_cost: float = 0.0
    # provider 真实请求数（V1-d 十三轮 P1）：一次逻辑调用 = 1 次基础请求 + schema_repairs
    # 次修复请求 + retries 次 transport 重试请求。与 logical_invocations（逻辑调用数）区分。
    v1_provider_requests: int = 0
    base_provider_requests: int = 0
    # 延迟（V1-d 十四轮 P1）：两组独立口径，不混用。
    # run_latency：以一次 V1/baseline 样本 repeat 为单位（端到端）；
    # provider_latency：以 usage 为单位（Provider 内部耗时）。保存总量+次数，报告层加权平均。
    v1_run_latency_ms: float = 0.0
    base_run_latency_ms: float = 0.0
    v1_run_failed_latency_ms: float = 0.0
    base_run_failed_latency_ms: float = 0.0
    v1_provider_latency_ms: float = 0.0
    base_provider_latency_ms: float = 0.0
    # 定价状态（V1-d 六轮 P1）：V1 与 baseline 分侧独立跟踪。
    # V1 走 GlobalBudget（可能 known/unknown/estimated/overrun），baseline 直拼不经
    # budget（由价格配置推导 known/unknown），不能共用同一字段——否则 V1 的 overrun
    # 会污染 baseline 的 known。
    # 初始 uninitialized（严重度最低），首次合并直接采用真实状态。
    v1_pricing_status: str = "uninitialized"
    base_pricing_status: str = "uninitialized"
    # 脱敏诊断轨迹（V1-d 八轮 P0）：候选/finding/expected 匹配，不含源码/凭证
    diagnostics: dict[str, Any] = field(default_factory=dict)
    failures: list[dict[str, str]] = field(default_factory=list)


def _redact_base_url(url: str) -> str:
    """剥离凭证与查询参数（与 smoke 同规则）。"""
    cleaned = url.split("?", 1)[0]
    for prefix in ("https://", "http://"):
        if cleaned.startswith(prefix):
            rest = cleaned[len(prefix) :]
            if "@" in rest:
                cleaned = prefix + rest.rsplit("@", 1)[-1]
            break
    return cleaned


def _redact_detail(text: str) -> str:
    """失败记录脱敏（should-fix）：剥离 API key / Bearer / 凭证片段，再截断。

    LLMRequestError 的 message 可能含响应文本前 200 字符，统一脱敏后只保留
    类型与安全摘要，避免把敏感片段写进报告。
    """
    cleaned = re.sub(r"(?i)(sk-[a-z0-9_-]{4,}|bearer\s+[a-z0-9._-]{4,})", "<redacted>", text)
    cleaned = re.sub(r"(?i)(authorization[=:]\s*)\S+", r"\1<redacted>", cleaned)
    return cleaned[:160]


async def _sample_diff(sample: EvalSample) -> tuple[str, dict[str, Any]]:
    """样本快照 → (diff 文本, file_map)。"""
    fake = FakeGitProvider()
    fake.add_snapshot("base", sample.base_files)
    fake.add_snapshot("head", sample.head_files)
    diff_text = await fake.get_diff("base", "head")
    files = parse_unified_diff(diff_text)
    filtered = filter_files(files, languages=["python"], max_files=40)
    file_map = {f.path: f for f in filtered.kept}
    return diff_text, file_map


async def _v1_review(
    sample: EvalSample,
    llm: LLMProvider,
    file_map: dict[str, Any],
    *,
    input_price: float | None,
    output_price: float | None,
) -> tuple[list[Finding], GlobalBudget, list[ModelUsage], list[str], list[FindingCandidate], list[Finding]]:
    """V1 完整链路：装配 → SinglePassReviewer → FindingPipeline → accepted findings。

    返回 (accepted, budget, usages, task_failures, candidates, findings)。
    candidates 为模型原始候选；findings 为 pipeline 全量定稿（含 suppressed/body_only，
    供诊断轨迹记录被抑制 reason，V1-d 八轮 P0）。task_failures 是预算拒绝/墙钟
    超时等被转成 FAILED 文件任务的脱敏摘要（BudgetExceeded 在 SinglePassReviewer
    内被吞并转 FAILED task，不向上抛，必须显式检查，否则 execution_completed 失真）。
    """
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
    run = ReviewRun(run_id=f"real-{sample.id}", strategy=ReviewStrategyName.SINGLE_PASS)
    assembler = ContextAssembler()
    units: list[ReviewUnit] = []
    for f in file_map.values():
        units.extend(assembler.build_file_units(run_id=run.run_id, change_request=req, file=f))
    budget = GlobalBudget()
    reviewer = SinglePassReviewer(
        llm,
        input_price_per_1k=input_price,
        output_price_per_1k=output_price,
    )
    result = await reviewer.execute(units, run, budget)
    task_failures = [
        _redact_detail(t.error or "")
        for t in result.source_run.tasks
        if t.status.value == "failed" and t.error
    ]
    pipeline = FindingPipeline(repo="eval-repo", head_sha="head", min_confidence=0.0)
    findings = pipeline.process(
        run_id=run.run_id,
        candidates=result.candidates,
        file_map=file_map,
    )
    accepted = [f for f in findings if f.status is FindingStatus.ACCEPTED]
    return accepted, budget, result.source_run.usages, task_failures, result.candidates, findings


async def _baseline_review(
    sample: EvalSample,
    llm: LLMProvider,
    diff_text: str,
    *,
    input_price: float | None,
    output_price: float | None,
) -> tuple[list[Finding], ModelUsage, float, list[FindingCandidate]]:
    """直拼 Prompt baseline：整个 diff 拼成一条 user 消息交给同一 LLM。

    不经过 V1 的上下文装配/重定位/去重；候选按 claimed 位置直接匹配
    （baseline_findings）。费用按实际 token 重算（与生产层同一口径）。
    返回 (findings, usage, cost, candidates)——candidates 为原始候选，供诊断轨迹。
    """
    messages = [
        {
            "role": "system",
            "content": (
                "你是资深代码审查员。请审查以下 git diff，找出其中的缺陷，"
                "按 JSON 信封输出 findings（claimed_path / claimed_start_line 必须指向 diff 中的实际位置）。"
            ),
        },
        {"role": "user", "content": diff_text or "(空 diff)"},
    ]
    candidates, usage = await llm.structured(messages)
    cost = _price_usage(usage, input_price, output_price)  # 统一定价（V1-d 十三轮 P1）
    usage.cost_usd = cost
    return baseline_findings(candidates, sample), usage, cost, candidates


def _metrics_dict(m: Metrics) -> dict[str, Any]:
    return {
        "precision": m.precision,
        "recall": m.recall,
        "f1": m.f1,
        "position_accuracy": m.position_accuracy,
        "negative_noise": m.negative_noise,
    }


def _usage_of_exc(exc: BaseException) -> ModelUsage | None:
    """从异常提取脱敏 usage（StructuredOutputError 携带，V1-d 十二轮 P1）。"""
    if isinstance(exc, StructuredOutputError):
        return exc.usage
    return None


def _price_usage(
    usage: ModelUsage,
    input_price: float | None,
    output_price: float | None,
) -> float:
    """统一 usage 定价（成功/失败同套，V1-d 十三轮 P1）。

    已配置价格 → 按 input/output token 重算真实费用；未定价 → 用 usage.cost_usd
    （观测值，通常 0）。成功与失败调用都必须走同一套，保证 total_cost_usd 口径一致。
    """
    if input_price is None or output_price is None:
        return usage.cost_usd
    computed = estimate_cost(usage.input_tokens, usage.output_tokens, input_price, output_price)
    assert computed is not None  # 价格已确认配置
    return computed


def _provider_requests(usage: ModelUsage) -> int:
    """一次逻辑调用实际触发的 HTTP 请求数（V1-d 十三轮 P1）。

    = 1（基础请求）+ schema_repairs（修复请求）+ retries（transport 重试请求）。
    StructuredOutputError 场景：首次失败 + 1 次修复 = 2 次请求，但只是 1 次逻辑调用。
    """
    return 1 + usage.schema_repairs + usage.retries


def _category_str(cat: Any) -> str:
    """category 归一为字符串（FindingCategory enum 或 str）。"""
    return cat.value if hasattr(cat, "value") else str(cat)


def _safe_path(path: str | None) -> str | None:
    """claimed_path 脱敏（V1-d 八轮安全审查）：模型自由文本，可能复述源码/凭证。

    只做长度截断 + 凭证正则清洗，不保证语义；canonical_path 来自 file_map 键，
    由数据集作者控制，无需此处理。
    """
    if path is None:
        return None
    cleaned = re.sub(r"(?i)(sk-[a-z0-9_-]{4,}|bearer\s+[a-z0-9._-]{4,})", "<redacted>", path)
    cleaned = re.sub(r"(?i)(authorization[=:]\s*)\S+", r"\1<redacted>", cleaned)
    return cleaned[:200]


def _candidate_trace(c: FindingCandidate) -> dict[str, Any]:
    """脱敏候选轨迹：仅 category/claimed 位置/confidence，不含源码/凭证。"""
    return {
        "category": _category_str(c.category),
        "claimed_path": _safe_path(c.claimed_path),
        "claimed_start_line": c.claimed_start_line,
        "confidence": c.confidence,
    }


def _finding_trace(f: Finding) -> dict[str, Any]:
    """脱敏 finding 轨迹：canonical 位置/status/最终 reason，不含源码/凭证。"""
    reason = ""
    if f.versions:
        reason = f.versions[-1].reason
    return {
        "category": _category_str(f.category),
        "claimed_path": _safe_path(f.claimed_path),
        "claimed_start_line": f.claimed_start_line,
        "canonical_path": f.canonical_path,
        "canonical_start_line": f.canonical_start_line,
        "confidence": f.confidence,
        "status": f.status.value,
        "reason": reason,
    }


def _expected_trace(sample: EvalSample) -> list[dict[str, Any]]:
    """脱敏 expected 清单：category/path/line。"""
    return [
        {"category": e.category, "path": e.path, "line": e.line}
        for e in sample.expected
    ]


def _matched_expected_ids(sample: EvalSample, accepted: list[Finding]) -> list[int]:
    """返回已命中的 expected 下标（与 compute_metrics 的 category+path+line 口径一致）。"""
    used = [False] * len(accepted)
    hit_ids: list[int] = []
    for i, exp in enumerate(sample.expected):
        exp_cat = exp.category or None  # 与 compute_metrics 的 exp_cat is not None 保护一致
        for j, f in enumerate(accepted):
            if used[j]:
                continue
            if exp_cat is not None and _category_str(f.category) != exp_cat:
                continue
            if exp.path and f.canonical_path != exp.path:
                continue
            if exp.line is not None and f.canonical_start_line != exp.line:
                continue
            used[j] = True
            hit_ids.append(i)
            break
    return hit_ids


async def _run_sample(
    sample: EvalSample,
    llm: LLMProvider,
    *,
    repeats: int,
    input_price: float | None,
    output_price: float | None,
) -> SampleResult:
    diff_text, file_map = await _sample_diff(sample)
    result = SampleResult(sample_id=sample.id)

    # ---- V1 链路（重复 repeats 次） ----
    v1_metrics: list[Metrics] = []
    v1_cost = 0.0
    for _ in range(repeats):
        t0 = time.perf_counter()
        try:
            findings, budget, usages, task_failures, candidates, all_findings = await _v1_review(
                sample, llm, file_map, input_price=input_price, output_price=output_price
            )
            # run 级端到端延迟（成功 repeat；总量，报告层加权平均）
            result.v1_run_latency_ms += (time.perf_counter() - t0) * 1000
            v1_metrics.append(compute_metrics(sample.expected, findings, sample_kind=sample.kind))
            # 按 outcome 区分成功/失败调用（V1-d 十二轮 P1：失败 usage 以 outcome=failed 计入）
            ok_usages = [u for u in usages if u.outcome.value != "failed"]
            failed_usages = [u for u in usages if u.outcome.value == "failed"]
            result.v1_model_calls += len(ok_usages)
            result.v1_input_tokens += sum(u.input_tokens for u in ok_usages)
            result.v1_output_tokens += sum(u.output_tokens for u in ok_usages)
            result.v1_retries += sum(u.retries for u in ok_usages)
            result.v1_schema_repairs += sum(u.schema_repairs for u in ok_usages)
            result.v1_provider_requests += sum(_provider_requests(u) for u in ok_usages)
            # provider 级延迟（usage 单位）：成功与失败 usage 都计入
            result.v1_provider_latency_ms += sum(u.latency_ms for u in ok_usages)
            result.v1_failed_calls += len(failed_usages)
            result.v1_failed_input_tokens += sum(u.input_tokens for u in failed_usages)
            result.v1_failed_output_tokens += sum(u.output_tokens for u in failed_usages)
            result.v1_failed_retries += sum(u.retries for u in failed_usages)
            result.v1_failed_schema_repairs += sum(u.schema_repairs for u in failed_usages)
            # 注意：V1 失败成本不在此累加——_review_unit 失败已 settle 进 budget.cost_used，
            # 下方 v1_cost += budget.cost_used 已含失败成本（V1-d 十三轮审查：避免双计）。
            # 失败 usage 的 provider latency 也不在此加——run 级端到端 latency 已含
            # 那次失败等待，重复加会双计（V1-d 十四轮 P1）。provider 级单独在下方累计。
            result.v1_provider_latency_ms += sum(u.latency_ms for u in failed_usages)
            result.v1_provider_requests += sum(_provider_requests(u) for u in failed_usages)
            v1_cost += budget.cost_used
            result.v1_successful_runs += 1
            result.v1_pricing_status = _merge_pricing(result.v1_pricing_status, budget.pricing_status)
            # 脱敏诊断轨迹（V1-d 八轮 P0）：记录候选、全量 finding（含 suppressed reason）、
            # expected 命中，便于归因漏报/分类不一致/位置错误/抑制原因。
            result.diagnostics["v1_candidates"] = [_candidate_trace(c) for c in candidates]
            result.diagnostics["v1_findings"] = [_finding_trace(f) for f in all_findings]
            result.diagnostics["expected"] = _expected_trace(sample)
            result.diagnostics["v1_matched_expected_ids"] = _matched_expected_ids(sample, findings)
            # 预算拒绝/墙钟超时等被转成 FAILED 文件任务（不向上抛）→ 必须记入 failures，
            # 否则 execution_completed 失真（V1-d 四轮 P2 should-fix）
            for detail in task_failures:
                result.failures.append({"side": "v1", "kind": "TaskFailed", "detail": detail})
        except (LLMRequestError, StructuredOutputError) as exc:
            # run 级端到端延迟：顶层异常（_v1_review 整体失败）也记录失败耗时
            result.v1_run_failed_latency_ms += (time.perf_counter() - t0) * 1000
            # P2（十四轮）：定价状态同步与 usage 是否存在无关——LLMRequestError（重试耗尽）
            # 不携带 usage，但仍应据价格配置同步 known/unknown，否则全失败保持 uninitialized。
            result.v1_pricing_status = _merge_pricing(
                result.v1_pricing_status,
                "known" if (input_price is not None and output_price is not None) else "unknown",
            )
            # 失败调用也要计入真实成本（V1-d 十二轮 P1：StructuredOutputError 携带 usage）
            fu = _usage_of_exc(exc)
            if fu is not None:
                result.v1_failed_calls += 1
                result.v1_failed_input_tokens += fu.input_tokens
                result.v1_failed_output_tokens += fu.output_tokens
                result.v1_failed_retries += fu.retries
                result.v1_failed_schema_repairs += fu.schema_repairs
                result.v1_failed_cost += _price_usage(fu, input_price, output_price)
                result.v1_provider_latency_ms += fu.latency_ms
                result.v1_provider_requests += _provider_requests(fu)
            result.failures.append({"side": "v1", "kind": type(exc).__name__, "detail": _redact_detail(str(exc))})
        except Exception as exc:  # noqa: BLE001 — 失败样本记录，不中断整体
            result.failures.append({"side": "v1", "kind": type(exc).__name__, "detail": _redact_detail(str(exc))})
    if v1_metrics:
        result.v1_metrics = Metrics(
            precision=sum(m.precision for m in v1_metrics) / len(v1_metrics),
            recall=sum(m.recall for m in v1_metrics) / len(v1_metrics),
            f1=sum(m.f1 for m in v1_metrics) / len(v1_metrics),
            position_accuracy=sum(m.position_accuracy for m in v1_metrics) / len(v1_metrics),
            negative_noise=max(m.negative_noise for m in v1_metrics),
            details={"sample_kind": sample.kind, "n_expected": len(sample.expected)},
        )
        result.v1_cost_total = v1_cost  # 总量：所有 repeats 累计

    # ---- baseline 直拼 Prompt（重复 repeats 次） ----
    base_metrics: list[Metrics] = []
    base_cost = 0.0
    for _ in range(repeats):
        t0 = time.perf_counter()
        try:
            findings, usage, cost, candidates = await _baseline_review(
                sample, llm, diff_text, input_price=input_price, output_price=output_price
            )
            # run 级端到端延迟（成功 repeat；总量）
            result.base_run_latency_ms += (time.perf_counter() - t0) * 1000
            base_metrics.append(compute_metrics(sample.expected, findings, sample_kind=sample.kind))
            result.base_model_calls += 1
            result.base_input_tokens += usage.input_tokens
            result.base_output_tokens += usage.output_tokens
            result.base_retries += usage.retries
            result.base_schema_repairs += usage.schema_repairs
            result.base_provider_requests += _provider_requests(usage)
            result.base_provider_latency_ms += usage.latency_ms
            base_cost += cost
            result.base_successful_runs += 1
            # baseline 不经过 GlobalBudget（无 overrun/estimated），定价状态由价格配置直接推导：
            # 已定价 → known，未定价 → unknown。分侧独立跟踪（V1-d 六轮 P1）。
            result.base_pricing_status = _merge_pricing(
                result.base_pricing_status,
                "known" if (input_price is not None and output_price is not None) else "unknown",
            )
            # 脱敏诊断轨迹（V1-d 八轮 P0）：baseline 侧候选（claimed 即 canonical，无重定位/抑制）
            result.diagnostics["base_candidates"] = [_candidate_trace(c) for c in candidates]
            result.diagnostics["base_findings"] = [_finding_trace(f) for f in findings]
            result.diagnostics["base_matched_expected_ids"] = _matched_expected_ids(sample, findings)
        except (LLMRequestError, StructuredOutputError) as exc:
            # run 级端到端延迟：顶层异常也记录失败耗时
            result.base_run_failed_latency_ms += (time.perf_counter() - t0) * 1000
            # P2（十四轮）：定价状态同步与 usage 是否存在无关。
            result.base_pricing_status = _merge_pricing(
                result.base_pricing_status,
                "known" if (input_price is not None and output_price is not None) else "unknown",
            )
            # 失败调用也要计入真实成本（V1-d 十二轮 P1：StructuredOutputError 携带 usage）
            fu = _usage_of_exc(exc)
            if fu is not None:
                result.base_failed_calls += 1
                result.base_failed_input_tokens += fu.input_tokens
                result.base_failed_output_tokens += fu.output_tokens
                result.base_failed_retries += fu.retries
                result.base_failed_schema_repairs += fu.schema_repairs
                result.base_failed_cost += _price_usage(fu, input_price, output_price)
                result.base_provider_latency_ms += fu.latency_ms
                result.base_provider_requests += _provider_requests(fu)
            result.failures.append({"side": "baseline", "kind": type(exc).__name__, "detail": _redact_detail(str(exc))})
        except Exception as exc:  # noqa: BLE001 — 失败样本记录，不中断整体
            result.failures.append({"side": "baseline", "kind": type(exc).__name__, "detail": _redact_detail(str(exc))})
    if base_metrics:
        result.base_metrics = Metrics(
            precision=sum(m.precision for m in base_metrics) / len(base_metrics),
            recall=sum(m.recall for m in base_metrics) / len(base_metrics),
            f1=sum(m.f1 for m in base_metrics) / len(base_metrics),
            position_accuracy=sum(m.position_accuracy for m in base_metrics) / len(base_metrics),
            negative_noise=max(m.negative_noise for m in base_metrics),
            details={"sample_kind": sample.kind, "n_expected": len(sample.expected)},
        )
        result.base_cost_total = base_cost  # 总量：所有 repeats 累计
    return result


def _avg(ms: list[Metrics], attr: str) -> float:
    return sum(getattr(m, attr) for m in ms) / len(ms) if ms else 0.0


def _avg_position(ms: list[Metrics]) -> float:
    pos = [m.position_accuracy for m in ms if m.details.get("n_expected", 0) > 0]
    return sum(pos) / len(pos) if pos else 0.0


# 质量门槛（V1-d 四轮 P2）：V1 侧宏平均需达到明确阈值才算质量通过
# （参考 11 §5 / ThresholdConfig：Precision ≥ 0.7、位置准确率 ≥ 0.8）
_QUALITY_PRECISION = 0.7
_QUALITY_RECALL = 0.7
_QUALITY_POSITION = 0.8


def _merge_pricing(current: str, status: str) -> str:
    """按严重度合并 pricing 状态：overrun > estimated > unknown > known > uninitialized。

    样本间/重复间状态不一致时取最严重者，避免 results[0] 代表全部的误导。
    uninitialized 为初始态（严重度最低），首次合并直接采用真实状态（V1-d 五轮 P1：
    此前默认 unknown 会吞掉 known）。
    """
    _SEVERITY = {"overrun": 3, "estimated": 2, "unknown": 1, "known": 0, "uninitialized": -1}
    if _SEVERITY.get(status, -1) > _SEVERITY.get(current, -1):
        return status
    return current


def _quality_gate_passed(v1_ok: list[Metrics]) -> bool:
    """质量门槛：V1 侧 precision/recall/位置准确率是否达标（与"无异常"分开）。"""
    if not v1_ok:
        return False
    return (
        _avg(v1_ok, "precision") >= _QUALITY_PRECISION
        and _avg(v1_ok, "recall") >= _QUALITY_RECALL
        and _avg_position(v1_ok) >= _QUALITY_POSITION
    )


def _comparison(v1_ok: list[Metrics], base_ok: list[Metrics]) -> str:
    """V1 相对 baseline：improved / equal / degraded（按 F1）。

    调用方须传入 paired-success 集合（V1-d 十二轮 P1），否则是对不同样本集合比较。
    """
    v1_f1 = _avg(v1_ok, "f1")
    base_f1 = _avg(base_ok, "f1")
    if v1_f1 > base_f1 + 0.01:
        return "improved"
    if v1_f1 < base_f1 - 0.01:
        return "degraded"
    return "equal"


def _pricing_agg(results: list[SampleResult], attr: str) -> str:
    """跨样本聚合定价状态：取最严重者（overrun > estimated > unknown > known > uninitialized）。

    attr 区分 v1_pricing_status / base_pricing_status（V1-d 六轮 P1：分侧独立聚合）。
    uninitialized（无任何成功调用）返回 unknown 兜底。
    """
    _SEVERITY = {"overrun": 3, "estimated": 2, "unknown": 1, "known": 0, "uninitialized": -1}
    if not results:
        return "unknown"
    worst = max(results, key=lambda r: _SEVERITY.get(getattr(r, attr), -1))
    value = getattr(worst, attr)
    return "unknown" if value == "uninitialized" else value


def _build_report(
    results: list[SampleResult],
    *,
    model: str,
    dataset: str,
    repeats: int,
    temperature: float,
    max_output_tokens: int,
    base_url: str,
) -> dict[str, Any]:
    v1_ok = [r.v1_metrics for r in results if r.v1_metrics is not None]
    base_ok = [r.base_metrics for r in results if r.base_metrics is not None]
    failures = [f for r in results for f in r.failures]
    # paired-success（V1-d 十二轮 P1）：只取 V1 与 baseline 都成功的相同样本，
    # comparison_result 必须基于该集合，避免对不同样本集合做宏平均得出伪结论。
    paired_v1 = [r.v1_metrics for r in results if r.v1_metrics is not None and r.base_metrics is not None]
    paired_base = [r.base_metrics for r in results if r.v1_metrics is not None and r.base_metrics is not None]
    paired_ids = [r.sample_id for r in results if r.v1_metrics is not None and r.base_metrics is not None]
    v1_missing = [r.sample_id for r in results if r.v1_metrics is None]
    base_missing = [r.sample_id for r in results if r.base_metrics is None]
    return {
        "model": model,
        "base_url": _redact_base_url(base_url),
        "dataset": dataset,
        "repeats": repeats,
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": {
            "v1": {
                "precision": round(_avg(v1_ok, "precision"), 3),
                "recall": round(_avg(v1_ok, "recall"), 3),
                "f1": round(_avg(v1_ok, "f1"), 3),
                "position_accuracy": round(_avg_position(v1_ok), 3),
                "negative_noise": float(sum(m.negative_noise for m in v1_ok)),
            },
            "baseline": {
                "precision": round(_avg(base_ok, "precision"), 3),
                "recall": round(_avg(base_ok, "recall"), 3),
                "f1": round(_avg(base_ok, "f1"), 3),
                "position_accuracy": round(_avg_position(base_ok), 3),
                "negative_noise": float(sum(m.negative_noise for m in base_ok)),
            },
        },
        # paired-success 汇总（V1-d 十二轮 P1）：只对两侧都成功的相同样本
        "paired_summary": {
            "sample_ids": paired_ids,
            "sample_count": len(paired_ids),
            "v1": {
                "precision": round(_avg(paired_v1, "precision"), 3),
                "recall": round(_avg(paired_v1, "recall"), 3),
                "f1": round(_avg(paired_v1, "f1"), 3),
                "position_accuracy": round(_avg_position(paired_v1), 3),
            },
            "baseline": {
                "precision": round(_avg(paired_base, "precision"), 3),
                "recall": round(_avg(paired_base, "recall"), 3),
                "f1": round(_avg(paired_base, "f1"), 3),
                "position_accuracy": round(_avg_position(paired_base), 3),
            },
        },
        "missing": {"v1": v1_missing, "baseline": base_missing},
        "cost": {
            "v1": {
                # 总量 = 成功成本 + 失败成本（V1-d 十三轮 P1：失败调用成本不漏记）
                "total_cost_usd": round(
                    sum(r.v1_cost_total + r.v1_failed_cost for r in results), 6
                ),
                # 每次逻辑调用平均成本（V1-d 十四轮审查）：分母统一为逻辑调用数
                # （成功+失败），不再混用 review 运行数与逻辑调用数两个单位。
                "avg_cost_per_invocation_usd": round(
                    sum(r.v1_cost_total + r.v1_failed_cost for r in results)
                    / max(1, sum(r.v1_model_calls + r.v1_failed_calls for r in results)),
                    6,
                ),
                "pricing_status": _pricing_agg(results, "v1_pricing_status"),
                # 逻辑调用数 vs provider 真实请求数（V1-d 十三轮 P1：一次逻辑调用含
                # 基础请求 + repair + retry 多次 HTTP 请求）
                "logical_invocations": sum(r.v1_model_calls + r.v1_failed_calls for r in results),
                "successful_logical_invocations": sum(r.v1_model_calls for r in results),
                "failed_logical_invocations": sum(r.v1_failed_calls for r in results),
                "provider_requests": sum(r.v1_provider_requests for r in results),
                "input_tokens": sum(r.v1_input_tokens + r.v1_failed_input_tokens for r in results),
                "output_tokens": sum(r.v1_output_tokens + r.v1_failed_output_tokens for r in results),
                "retries": sum(r.v1_retries + r.v1_failed_retries for r in results),
                "schema_repairs": sum(r.v1_schema_repairs + r.v1_failed_schema_repairs for r in results),
            },
            "baseline": {
                "total_cost_usd": round(
                    sum(r.base_cost_total + r.base_failed_cost for r in results), 6
                ),
                "avg_cost_per_invocation_usd": round(
                    sum(r.base_cost_total + r.base_failed_cost for r in results)
                    / max(1, sum(r.base_model_calls + r.base_failed_calls for r in results)),
                    6,
                ),
                "pricing_status": _pricing_agg(results, "base_pricing_status"),
                "logical_invocations": sum(r.base_model_calls + r.base_failed_calls for r in results),
                "successful_logical_invocations": sum(r.base_model_calls for r in results),
                "failed_logical_invocations": sum(r.base_failed_calls for r in results),
                "provider_requests": sum(r.base_provider_requests for r in results),
                "input_tokens": sum(r.base_input_tokens + r.base_failed_input_tokens for r in results),
                "output_tokens": sum(r.base_output_tokens + r.base_failed_output_tokens for r in results),
                "retries": sum(r.base_retries + r.base_failed_retries for r in results),
                "schema_repairs": sum(r.base_schema_repairs + r.base_failed_schema_repairs for r in results),
            },
        },
        "latency": {
            # 延迟口径（V1-d 十四轮 P1）：两组独立口径，各自总量/次数/加权平均。
            # - run_latency：一次样本 repeat 的端到端耗时（成功 repeat 总量 / 成功次数）；
            # - provider_latency：usage 级 Provider 耗时（总量 / provider 请求数）。
            # 不再把"样本平均的平均"当 attempted，也不把失败 usage 耗时重复加入 run 级。
            "v1": {
                "run_latency": {
                    "success_total_ms": round(sum(r.v1_run_latency_ms for r in results), 2),
                    "success_count": sum(r.v1_successful_runs for r in results),
                    "success_avg_ms": round(
                        sum(r.v1_run_latency_ms for r in results)
                        / max(1, sum(r.v1_successful_runs for r in results)),
                        2,
                    ),
                    "failed_total_ms": round(sum(r.v1_run_failed_latency_ms for r in results), 2),
                },
                "provider_latency": {
                    "total_ms": round(sum(r.v1_provider_latency_ms for r in results), 2),
                    # usage.latency_ms 是每次逻辑调用（structured 入口→返回）总耗时，
                    # 分母应为逻辑调用数（成功+失败 usage），不是 provider HTTP 请求数——
                    # 否则有 schema_repairs/retries 时会把单次逻辑耗时摊薄（V1-d 十四轮审查）。
                    "invocation_count": sum(r.v1_model_calls + r.v1_failed_calls for r in results),
                    "avg_ms": round(
                        sum(r.v1_provider_latency_ms for r in results)
                        / max(1, sum(r.v1_model_calls + r.v1_failed_calls for r in results)),
                        2,
                    ),
                },
            },
            "baseline": {
                "run_latency": {
                    "success_total_ms": round(sum(r.base_run_latency_ms for r in results), 2),
                    "success_count": sum(r.base_successful_runs for r in results),
                    "success_avg_ms": round(
                        sum(r.base_run_latency_ms for r in results)
                        / max(1, sum(r.base_successful_runs for r in results)),
                        2,
                    ),
                    "failed_total_ms": round(sum(r.base_run_failed_latency_ms for r in results), 2),
                },
                "provider_latency": {
                    "total_ms": round(sum(r.base_provider_latency_ms for r in results), 2),
                    "invocation_count": sum(r.base_model_calls + r.base_failed_calls for r in results),
                    "avg_ms": round(
                        sum(r.base_provider_latency_ms for r in results)
                        / max(1, sum(r.base_model_calls + r.base_failed_calls for r in results)),
                        2,
                    ),
                },
            },
        },
        "per_sample": [
            {
                "id": r.sample_id,
                "v1": None if r.v1_metrics is None else _metrics_dict(r.v1_metrics),
                "baseline": None if r.base_metrics is None else _metrics_dict(r.base_metrics),
                "v1_model_calls": r.v1_model_calls,
                "v1_input_tokens": r.v1_input_tokens,
                "v1_output_tokens": r.v1_output_tokens,
                "v1_retries": r.v1_retries,
                "v1_schema_repairs": r.v1_schema_repairs,
                "v1_cost_total_usd": r.v1_cost_total,
                "baseline_model_calls": r.base_model_calls,
                "baseline_input_tokens": r.base_input_tokens,
                "baseline_output_tokens": r.base_output_tokens,
                "baseline_retries": r.base_retries,
                "baseline_schema_repairs": r.base_schema_repairs,
                "baseline_cost_total_usd": r.base_cost_total,
                "v1_pricing_status": r.v1_pricing_status,
                "base_pricing_status": r.base_pricing_status,
                "diagnostics": r.diagnostics,
                "failures": r.failures,
            }
            for r in results
        ],
        "failures": failures,
        "execution_completed": not failures,
        "quality_gate_passed": _quality_gate_passed(v1_ok),
        # paired-success 口径（V1-d 十二轮 P1）
        "comparison_result": _comparison(paired_v1, paired_base),
    }


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 真实模型 V1 vs 直拼 Prompt baseline 对照",
        "",
        f"> 模型 `{report['model']}`  dataset `{report['dataset']}`  repeats={report['repeats']}  "
        f"temperature={report['temperature']}  max_output_tokens={report['max_output_tokens']}",
        "",
        "## 质量（macro 平均）",
        "",
        "| 指标 | V1 | baseline |",
        "|------|----|----------|",
    ]
    s = report["summary"]
    for k in ("precision", "recall", "f1", "position_accuracy", "negative_noise"):
        lines.append(f"| {k} | {s['v1'][k]} | {s['baseline'][k]} |")
    lines.append("")
    lines.append("## 成本 / 延迟")
    lines.append("")
    lines.append("| 项 | V1 | baseline |")
    lines.append("|----|----|----------|")
    c, lat = report["cost"], report["latency"]
    lines.append(f"| total_cost_usd | {c['v1']['total_cost_usd']} ({c['v1']['pricing_status']}) | {c['baseline']['total_cost_usd']} ({c['baseline']['pricing_status']}) |")
    lines.append(f"| avg_cost_per_invocation_usd | {c['v1']['avg_cost_per_invocation_usd']} | {c['baseline']['avg_cost_per_invocation_usd']} |")
    lines.append(f"| logical_invocations | {c['v1']['logical_invocations']} | {c['baseline']['logical_invocations']} |")
    lines.append(f"| provider_requests | {c['v1']['provider_requests']} | {c['baseline']['provider_requests']} |")
    lines.append(f"| failed_logical_invocations | {c['v1']['failed_logical_invocations']} | {c['baseline']['failed_logical_invocations']} |")
    lines.append(f"| input_tokens | {c['v1']['input_tokens']} | {c['baseline']['input_tokens']} |")
    lines.append(f"| output_tokens | {c['v1']['output_tokens']} | {c['baseline']['output_tokens']} |")
    lines.append(f"| retries | {c['v1']['retries']} | {c['baseline']['retries']} |")
    lines.append(f"| schema_repairs | {c['v1']['schema_repairs']} | {c['baseline']['schema_repairs']} |")
    lines.append(f"| 延迟(run 成功)均值 ms | {lat['v1']['run_latency']['success_avg_ms']} | {lat['baseline']['run_latency']['success_avg_ms']} |")
    lines.append(f"| 延迟(run 失败)总计 ms | {lat['v1']['run_latency']['failed_total_ms']} | {lat['baseline']['run_latency']['failed_total_ms']} |")
    lines.append(f"| 延迟(provider)均值 ms | {lat['v1']['provider_latency']['avg_ms']} | {lat['baseline']['provider_latency']['avg_ms']} |")
    lines.append("")
    # paired-success 与 missing（V1-d 十二轮 P1）
    ps = report["paired_summary"]
    lines.append(f"> paired-success: {ps['sample_count']} 样本（{', '.join(ps['sample_ids']) or '无'}）")
    if report["missing"]["v1"]:
        lines.append(f"> V1 缺失样本: {', '.join(report['missing']['v1'])}")
    if report["missing"]["baseline"]:
        lines.append(f"> baseline 缺失样本: {', '.join(report['missing']['baseline'])}")
    lines.append("")
    lines.append(f"> execution_completed={report['execution_completed']}  "
                 f"quality_gate_passed={report['quality_gate_passed']}  "
                 f"comparison_result={report['comparison_result']}")
    if report["failures"]:
        lines.append("")
        lines.append("## 失败样本（脱敏）")
        lines.append("")
        for f in report["failures"]:
            lines.append(f"- `{f['side']}` {f['kind']}: {f['detail']}")
    return "\n".join(lines)


def _resolve_output_paths(
    *,
    sample_ids: str,
    output: str | None,
    markdown: str | None,
    matched_ids: list[str],
) -> tuple[str, str]:
    """决定 JSON/Markdown 输出路径（V1-d 十三轮 P2）。

    - 指定 --sample-ids 且未显式传 output/markdown → 自动用带样本 id 的 debug 路径，
      防止单样本调试覆盖完整正式证据；
    - 完整运行（无 --sample-ids）→ 正式路径。
    显式传入的 output/markdown 始终优先。
    """
    if sample_ids:
        slug = "-".join(sorted(matched_ids))
        return (
            output if output is not None else f"docs/evidence/v1-d-debug-{slug}.json",
            markdown if markdown is not None else f"docs/evidence/v1-d-debug-{slug}.md",
        )
    return (
        output if output is not None else "docs/evidence/v1-d-real-compare.json",
        markdown if markdown is not None else "docs/evidence/v1-d-real-compare.md",
    )


async def _main(args: argparse.Namespace) -> int:
    settings = Settings()
    api_key = os.environ.get(settings.llm.api_key_env, "")
    base_url = os.environ.get(settings.llm.base_url_env, "")
    if not api_key or not base_url:
        print(f"缺少配置：请设置 {settings.llm.api_key_env} 与 {settings.llm.base_url_env} 环境变量")
        return 1
    model_name = os.environ.get("MODEL_NAME") or settings.llm.model
    llm_cfg = settings.llm.model_copy(update={"model": model_name})
    ds = EvalDataset.load_yaml(Path(args.dataset))
    samples = ds.samples
    # P2（V1-d 十三轮）：单样本调试不得覆盖完整正式证据。--sample-ids 指定时，
    # 若 output/markdown 未显式传入（仍为 None），自动改用带样本 id 的 debug 文件名。
    if args.sample_ids:
        wanted = {s.strip() for s in args.sample_ids.split(",") if s.strip()}
        samples = [s for s in ds.samples if s.id in wanted]
        if not samples:
            print(f"未匹配任何样本：{args.sample_ids}")
            return 1
        print(f"仅运行样本子集: {', '.join(s.id for s in samples)}")
    args.output, args.markdown = _resolve_output_paths(
        sample_ids=args.sample_ids,
        output=args.output,
        markdown=args.markdown,
        matched_ids=[s.id for s in samples],
    )
    print(f"真实对照: model={llm_cfg.model} samples={len(samples)} repeats={args.repeats}")

    input_price = settings.llm.input_price_per_1k
    output_price = settings.llm.output_price_per_1k
    if input_price is None or output_price is None:
        print("注意：llm 未配置价格（input/output_price_per_1k）→ 费用标记 unknown")

    provider = OpenAICompatProvider.from_config(llm_cfg)
    try:
        results = []
        for sample in samples:
            print(f"  样本 {sample.id} ...")
            results.append(
                await _run_sample(
                    sample,
                    provider,
                    repeats=args.repeats,
                    input_price=input_price,
                    output_price=output_price,
                )
            )
    finally:
        await provider.aclose()

    report = _build_report(
        results,
        model=llm_cfg.model,
        dataset=args.dataset,
        repeats=args.repeats,
        temperature=llm_cfg.temperature,
        max_output_tokens=llm_cfg.max_output_tokens,
        base_url=base_url,
    )
    if args.output:
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"JSON 报告已写入: {args.output}")
    if args.markdown:
        Path(args.markdown).write_text(_render_markdown(report), encoding="utf-8")
        print(f"Markdown 报告已写入: {args.markdown}")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(
        f"execution_completed={report['execution_completed']} "
        f"quality_gate_passed={report['quality_gate_passed']} "
        f"comparison_result={report['comparison_result']}"
    )
    if not report["execution_completed"]:
        print(f"存在 {len(report['failures'])} 条失败记录（详见报告）")
        return 2  # 执行不完整
    if not report["quality_gate_passed"]:
        print("V1 质量门槛未达标（precision/recall/位置准确率低于阈值）")
        return 3  # 质量门槛失败
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="真实模型 V1 vs 直拼 Prompt baseline 对照")
    parser.add_argument("--dataset", default=_DEFAULT_DATASET)
    parser.add_argument("--repeats", type=int, default=_REPEATS)
    parser.add_argument("--sample-ids", default="", help="逗号分隔的样本 id 子集（调试单个样本用，V1-d 十二轮 P2）")
    parser.add_argument("--output", default=None, help="JSON 报告路径（默认按是否 --sample-ids 自动选择 debug/正式路径）")
    parser.add_argument("--markdown", default=None, help="Markdown 报告路径（同上）")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
