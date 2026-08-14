"""OQ-1 最小实测入口（14 §2：DP-V4-PRO 能力实测，配置 key 后运行）。

用法::

    set MODEL_BASE_URL=https://your-endpoint/v1
    set MODEL_API_KEY=sk-...
    python -m reposage.providers.llm.smoke

验证项（OQ-1）：
1. OpenAI-compatible API 连通性（最小请求）；
2. 异步调用与基本延迟；
3. 稳定 JSON（结构化信封解析成功率，重复 N 次）；
4. usage/cost 记账是否返回。

退出码 0=通过，1=失败。未配置环境变量时给出明确指引（启动即失败，09 §3）。
"""

from __future__ import annotations

import asyncio
import os
import sys

from reposage.config.settings import Settings
from reposage.providers.llm.openai_compat import OpenAICompatProvider, StructuredOutputError


def _print_check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


async def _run(rounds: int = 5) -> int:
    settings = Settings()
    api_key = os.environ.get(settings.llm.api_key_env, "")
    base_url = os.environ.get(settings.llm.base_url_env, "")
    if not api_key or not base_url:
        print(
            f"缺少配置：请设置 {settings.llm.api_key_env} 与 {settings.llm.base_url_env} 环境变量"
        )
        return 1
    provider = OpenAICompatProvider.from_config(settings.llm)

    # 1. 最小请求（连通性 + 延迟）
    t0 = asyncio.get_event_loop().time()
    resp = await provider.complete(
        [{"role": "user", "content": "回复 OK 即可"}],
        temperature=0.0,
    )
    latency = (asyncio.get_event_loop().time() - t0) * 1000
    ok = bool(resp.text) and resp.usage is not None
    _print_check("最小请求连通", ok, f"latency={latency:.0f}ms usage={resp.usage}")
    if not ok:
        return 1

    # 2. 稳定 JSON：重复 rounds 次结构化请求，统计解析成功率
    parsed = 0
    schema_failures = 0
    for i in range(rounds):
        try:
            findings, usage = await provider.structured(
                [{"role": "user", "content": "这个 PR 没有值得审查的问题，返回空 findings。"}]
            )
            parsed += 1
        except StructuredOutputError as exc:
            schema_failures += 1
            print(f"   round {i + 1} 解析失败: {exc.detail[:120]}")
        except Exception as exc:  # noqa: BLE001
            print(f"   round {i + 1} 请求失败: {type(exc).__name__}: {exc}")
            return 1
    rate = parsed / rounds if rounds else 0.0
    _print_check(f"结构化 JSON 解析成功率 ({parsed}/{rounds})", rate >= 0.8, f"{rate:.0%}")
    if schema_failures:
        print(f"   字段漂移/失败 {schema_failures} 次（schema_first 策略，见 09 §3）")
    return 0 if rate >= 0.8 else 1


def main() -> int:
    rounds = int(os.environ.get("OQ1_ROUNDS", "5"))
    try:
        return asyncio.run(_run(rounds))
    except KeyboardInterrupt:
        print("已取消")
        return 130


if __name__ == "__main__":
    sys.exit(main())
