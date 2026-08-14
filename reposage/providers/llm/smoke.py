"""OQ-1 最小实测入口（14 §2：DP-V4-PRO 能力实测，配置 key 后运行）。

用法::

    set MODEL_BASE_URL=https://your-endpoint/v1
    set MODEL_API_KEY=sk-...
    set OQ1_ROUNDS=20          # 默认 20（架构要求重复 20 次测稳定 JSON）
    set OQ1_REPORT=docs/evidence/v1-c-dp-v4-pro-smoke.json   # 可选：写结构化报告
    python -m reposage.providers.llm.smoke

验证项（OQ-1）：
1. OpenAI-compatible API 连通性（最小请求 + 延迟）；
2. 异步并发 3 请求（延迟与 429 观察）；
3. 稳定 JSON：重复 OQ1_ROUNDS 次结构化请求，统计解析成功率与字段漂移/失败原因；
4. usage/cost 记账（未定价标 0）与重试/修复统计（429、http_retries、repairs）。

退出码：0=通过（结构化成功率 >= 0.8），1=失败/未配置。client 无论成败均关闭
（async with / finally）。报告 JSON 已脱敏（不含 api key / Authorization / 请求体）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

from reposage.config.settings import Settings
from reposage.domain.models import ModelUsage
from reposage.providers.llm.openai_compat import (
    LLMRequestError,
    OpenAICompatProvider,
    StructuredOutputError,
)

DEFAULT_ROUNDS = 20  # 架构要求重复 20 次测稳定 JSON（14 OQ-1）
CONCURRENCY = 3  # 并发 3 请求测延迟/429（04 §6）


def _mark(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


async def _run(
    rounds: int,
    report_path: str | None,
    *,
    provider_factory: Any = None,  # 可注入假 provider（测试）；默认 OpenAICompatProvider.from_config
) -> int:
    """OQ-1 实测主流程。

    - 模型名优先级：``MODEL_NAME`` 环境变量 > ``llm.model`` 配置 > 内置默认值
      （验收 Round 1：真实服务端契约要求可覆盖模型名，如 deepseek-v4-pro）；
    - 启动时打印实际模型名，但绝不打印 API key；
    - try/finally：配置了 report_path 时成功/失败均落盘脱敏报告（P2-4）；
    - mandatory checks（minimal / concurrency / structured）：任一失败整体 exit 1；
    - 报告包含 overall ``passed`` 字段，顶层 ``model`` 与实际请求模型一致。
    """
    settings = Settings()
    api_key = os.environ.get(settings.llm.api_key_env, "")
    base_url = os.environ.get(settings.llm.base_url_env, "")
    if not api_key or not base_url:
        print(f"缺少配置：请设置 {settings.llm.api_key_env} 与 {settings.llm.base_url_env} 环境变量")
        return 1

    # MODEL_NAME 覆盖（不硬编码具体服务商模型名，保留 OpenAI-compatible 可配置性）
    model_name = os.environ.get("MODEL_NAME") or settings.llm.model
    llm = settings.llm.model_copy(update={"model": model_name})
    print(f"模型: {llm.model}（未设置 MODEL_NAME 时使用配置 llm.model 默认值）")

    factory = provider_factory or OpenAICompatProvider.from_config
    report: dict[str, Any] = {
        "model": llm.model,  # 顶层 model = 实际请求模型（Round 1 要求一致）
        "base_url": _redact_base_url(base_url),
        "rounds": rounds,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "checks": {},
        "failures": [],
        "passed": False,
    }
    exit_code = 1
    try:
        async with factory(llm) as provider:  # 自建 client 一定关闭；llm.model 已被 MODEL_NAME 覆盖
            loop = asyncio.get_event_loop()

            # 1. 最小请求（mandatory）
            minimal_ok = False
            t0 = loop.time()
            try:
                resp = await provider.complete(
                    [{"role": "user", "content": "回复 OK 即可"}], temperature=0.0
                )
                latency_ms = int((loop.time() - t0) * 1000)
                minimal_ok = bool(resp.text) and resp.usage is not None
                report["checks"]["minimal_request"] = {
                    "ok": minimal_ok,
                    "latency_ms": latency_ms,
                    "usage": None if resp.usage is None else _usage_dict(resp.usage),
                }
                print(f"[{_mark(minimal_ok)}] 最小请求连通 latency={latency_ms}ms usage={resp.usage}")
            except LLMRequestError as exc:
                report["checks"]["minimal_request"] = {"ok": False, "error": str(exc)[:200]}
                print(f"[FAIL] 最小请求失败: {exc}")

            # 2. 并发 3 请求（mandatory）
            concurrency_ok = False
            t0 = loop.time()
            try:
                await asyncio.gather(
                    *(provider.complete([{"role": "user", "content": "并发探针"}]) for _ in range(CONCURRENCY))
                )
                concurrency_ms = int((loop.time() - t0) * 1000)
                concurrency_ok = True
                report["checks"]["concurrency_3"] = {"ok": True, "total_ms": concurrency_ms}
                print(f"[{_mark(True)}] 并发 3 请求 total={concurrency_ms}ms")
            except LLMRequestError as exc:
                report["checks"]["concurrency_3"] = {"ok": False, "error": str(exc)[:200]}
                print(f"[FAIL] 并发 3 请求失败: {exc}")

            # 3. 稳定 JSON（mandatory，阈值 0.8）
            parsed = 0
            structure_failures = 0
            request_failures = 0
            for i in range(rounds):
                try:
                    findings, usage = await provider.structured(
                        [{"role": "user", "content": "这个 PR 没有值得审查的问题，返回空 findings。"}]
                    )
                    parsed += 1
                except StructuredOutputError as exc:
                    structure_failures += 1
                    report["failures"].append(
                        {"round": i + 1, "kind": "structure", "detail": exc.detail[:160]}
                    )
                    print(f"   round {i + 1} 结构失败: {exc.detail[:120]}")
                except LLMRequestError as exc:
                    request_failures += 1
                    report["failures"].append(
                        {"round": i + 1, "kind": "request", "detail": str(exc)[:160]}
                    )
                    print(f"   round {i + 1} 请求失败: {exc}")
            rate = parsed / rounds if rounds else 0.0
            structured_ok = rate >= 0.8
            report["checks"]["structured"] = {
                "ok": structured_ok,
                "parsed": parsed,
                "total": rounds,
                "rate": round(rate, 4),
                "structure_failures": structure_failures,
                "request_failures": request_failures,
                "repairs": provider.stats["repairs"],
                "schema_fallbacks": provider.stats["schema_fallbacks"],
                "response_format_fallbacks": provider.stats["response_format_fallbacks"],
                "http_attempts": provider.stats["http_attempts"],
                "http_retryable_failures": provider.stats["http_retryable_failures"],
                "http_retries": provider.stats["http_retries"],
                "http_429": provider.stats["http_429"],
            }
            print(
                f"[{_mark(structured_ok)}] 结构化解析成功率 {parsed}/{rounds} = {rate:.0%} "
                f"(修复 {provider.stats['repairs']} 次 / 重试 {provider.stats['http_retries']} 次 / 429 {provider.stats['http_429']} 次)"
            )
            if structure_failures:
                print(f"   字段漂移/失败 {structure_failures} 次（schema_first 策略，见 09 §3）")

            # 汇总 mandatory checks
            exit_code = 0 if (minimal_ok and concurrency_ok and structured_ok) else 1
            report["passed"] = exit_code == 0
    finally:
        # 4. 报告（脱敏）落盘：成功/失败均执行（P2-4）
        if report_path:
            with open(report_path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
            print(f"报告已写入: {report_path}  passed={report['passed']}")
    return exit_code


def _usage_dict(usage: ModelUsage) -> dict[str, Any]:
    return {
        "model": usage.model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": usage.cost_usd,
        "latency_ms": usage.latency_ms,
    }


def _redact_base_url(base_url: str) -> str:
    """脱敏 base_url：去掉可能携带的凭证片段（查询参数/userinfo）。"""
    redacted = base_url.split("?")[0]
    if "://" in redacted:
        scheme, rest = redacted.split("://", 1)
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        return f"{scheme}://{rest}"
    return redacted


def main() -> int:
    rounds = int(os.environ.get("OQ1_ROUNDS", str(DEFAULT_ROUNDS)))  # 默认 20 轮
    report_path = os.environ.get("OQ1_REPORT")
    try:
        return asyncio.run(_run(rounds, report_path))
    except KeyboardInterrupt:
        print("已取消")
        return 130


if __name__ == "__main__":
    sys.exit(main())
