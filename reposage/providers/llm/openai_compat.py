"""OpenAI-compatible LLM Provider（providers/llm/openai_compat.py，09 §3 / 14 OQ-1）。

只依赖 OpenAI-compatible ``/chat/completions`` 协议；base_url/api_key 从环境读取
（LLMConfig.api_key_env / base_url_env）。

结构化输出策略（09 §3）：
- schema_first：优先 ``response_format={"type": "json_schema"}``；若端点返回 400
  （不支持 json_schema），降级 ``{"type": "json_object"}`` + 提示内嵌 schema；
- json_repair：不依赖 response_format，普通输出 + 解析；
- 两种策略统一走：解析 → Pydantic 严格校验 → 单次修复重试（把校验错误回喂）→
  仍失败抛 StructuredOutputError（由调用方 fail-soft 并记 Coverage）。

可靠性：网络错误/429/5xx 指数退避重试（max_retries）；4xx 非 429 视为永久错误。
成本：usage token 精确记账；cost_usd 未定价标 0（V1-f 成本监控补价格）。
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from reposage.config.settings import LLMConfig
from reposage.domain.finding import FindingCandidate
from reposage.domain.models import ModelUsage
from reposage.domain.protocols import ModelResponse

from .schema import envelope_json_schema, parse_envelope

# 结构化系统指令：要求只输出 JSON 信封（json_repair / 降级路径用）
_SYSTEM_JSON_INSTRUCTION = """\
你只输出一个 JSON 对象，不要输出任何其他文本、Markdown 代码块或解释。
信封格式：{"findings": [{"title": str, "severity": "critical|high|medium|low|info", \
"confidence": float(0-1), "category": "correctness|security|silent_failure|concurrency|edge_case|test_gap|performance|maintainability", \
"claimed_path": str|null, "claimed_start_line": int|null, "claimed_end_line": int|null, \
"chunk_id": str|null, "evidence_ref": str|null, "trigger_condition": str, "impact": str, "explanation": str, "suggestion": str, "is_outside_diff": bool}], \
"summary": {}}"""


class LLMRequestError(RuntimeError):
    """网络/HTTP 层失败（重试耗尽或 4xx 永久错误）。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class StructuredOutputError(RuntimeError):
    """结构化输出解析/校验失败（修复重试后仍失败；调用方 fail-soft）。"""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class OpenAICompatProvider:
    """OpenAI-compatible 提供者（httpx 异步客户端，可注入 MockTransport 测试）。"""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        temperature: float = 0.1,
        timeout_seconds: int = 60,
        max_retries: int = 2,
        structured_strategy: str = "schema_first",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key 不能为空（请设置 LLMConfig.api_key_env 对应环境变量）")
        if not base_url:
            raise ValueError("base_url 不能为空（请设置 LLMConfig.base_url_env 对应环境变量）")
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries
        self.structured_strategy = structured_strategy
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    @classmethod
    def from_config(
        cls,
        llm: LLMConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> OpenAICompatProvider:
        """从 LLMConfig 构造；api_key/base_url 读环境变量（09 §3：非法配置启动即失败）。"""
        api_key = os.environ.get(llm.api_key_env, "")
        base_url = os.environ.get(llm.base_url_env, "")
        if not api_key:
            raise ValueError(
                f"缺少 API key：环境变量 {llm.api_key_env} 未设置"
            )
        if not base_url:
            raise ValueError(
                f"缺少 base_url：环境变量 {llm.base_url_env} 未设置"
            )
        return cls(
            model=llm.model,
            api_key=api_key,
            base_url=base_url,
            temperature=llm.temperature,
            timeout_seconds=llm.timeout_seconds,
            max_retries=llm.max_retries,
            structured_strategy=llm.structured_strategy,
            http_client=http_client,
        )

    # ---- 通用补全 ----

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.1,
    ) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if schema is not None:
            body["response_format"] = {"type": "json_object"}
        data, usage = await self._post(body)
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return ModelResponse(text=text, usage=self._make_usage(usage))

    # ---- 结构化输出（V1 reviewer 用，09 §3） ----

    async def structured(
        self,
        messages: list[dict[str, Any]],
        *,
        prompt_hash: str | None = None,
        schema_hash: str | None = None,
    ) -> tuple[list[FindingCandidate], ModelUsage]:
        """结构化审查输出：schema_first/json_repair + 单次修复重试 → fail-soft 抛错。"""
        started = time.monotonic()
        total_usage: dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0}
        if self.structured_strategy == "schema_first":
            text = await self._chat_structured_schema_first(messages, total_usage)
        else:
            text = await self._chat_structured_plain(messages, total_usage)

        try:
            envelope = parse_envelope(text)
        except Exception as exc:  # JSONDecodeError / ValidationError / ValueError
            # 单次修复重试：把错误回喂
            detail = f"{type(exc).__name__}: {exc}"
            text2 = await self._chat_structured_plain(
                [*messages, {"role": "user", "content": _REPAIR_INSTRUCTION.format(detail=detail)}],
                total_usage,
            )
            try:
                envelope = parse_envelope(text2)
            except Exception as exc2:
                raise StructuredOutputError(
                    "结构化输出解析失败（修复重试后仍失败）",
                    detail=f"{type(exc2).__name__}: {exc2}",
                ) from exc2
        latency_ms = int((time.monotonic() - started) * 1000)
        return envelope.findings, self._make_usage(total_usage, latency_ms=latency_ms)

    async def _chat_structured_schema_first(self, messages: list[dict[str, Any]], usage_agg: dict[str, Any]) -> str:
        """schema_first：json_schema → 400 降级 json_object + 内嵌 schema。"""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [*messages, {"role": "system", "content": _SYSTEM_JSON_INSTRUCTION}],
            "temperature": self.temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "findings_envelope", "schema": envelope_json_schema()},
            },
        }
        try:
            data, usage = await self._post(body)
        except LLMRequestError as exc:
            if exc.status_code == 400:  # 端点不支持 json_schema → 降级 json_object
                body["response_format"] = {"type": "json_object"}
                data, usage = await self._post(body)
            else:
                raise
        _accumulate_usage(usage_agg, usage)
        return _content_of(data)

    async def _chat_structured_plain(self, messages: list[dict[str, Any]], usage_agg: dict[str, Any]) -> str:
        """json_repair / 修复重试：普通输出 + 提示内嵌 schema。"""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [*messages, {"role": "system", "content": _SYSTEM_JSON_INSTRUCTION}],
            "temperature": self.temperature,
        }
        data, usage = await self._post(body)
        _accumulate_usage(usage_agg, usage)
        return _content_of(data)

    # ---- V3 占位 ----

    async def tool_loop(
        self,
        session: object,
        tools: list[dict[str, Any]],
        *,
        budget: Any,
    ) -> ModelResponse:
        raise NotImplementedError("tool_loop 属 V3（08 §9 方案 A/B），V1 不实现")

    # ---- 内部 ----

    async def _post(self, body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """POST /chat/completions；网络错误/429/5xx 指数退避重试，4xx 非 429 不重试。"""
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:
                last_error = exc
                await _backoff(attempt)
                continue
            if resp.status_code == 200:
                data = resp.json()
                return data, data.get("usage", {})
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = LLMRequestError(f"HTTP {resp.status_code}", status_code=resp.status_code)
                retry_after = resp.headers.get("retry-after")
                await _backoff(attempt, retry_after=retry_after)
                continue
            raise LLMRequestError(
                f"HTTP {resp.status_code}: {resp.text[:200]}", status_code=resp.status_code
            )
        raise LLMRequestError(f"重试耗尽（{self.max_retries} 次）: {last_error}")

    def _make_usage(self, usage: dict[str, Any], *, latency_ms: int = 0) -> ModelUsage:
        return ModelUsage(
            model=self.model,
            role="general",
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            cost_usd=0.0,  # 未定价（V1-f 成本监控补价格）
            latency_ms=latency_ms,
        )


_REPAIR_INSTRUCTION = (
    "你上次的输出无法解析为合法 JSON 信封，错误如下：\n{detail}\n"
    "请只输出一个修正后的 JSON 对象，严格遵守信封格式与枚举白名单，不要任何多余文本。"
)


def _content_of(data: dict[str, Any]) -> str:
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


def _accumulate_usage(agg: dict[str, Any], usage: dict[str, Any]) -> None:
    agg["prompt_tokens"] = int(agg.get("prompt_tokens", 0)) + int(usage.get("prompt_tokens", 0) or 0)
    agg["completion_tokens"] = int(agg.get("completion_tokens", 0)) + int(
        usage.get("completion_tokens", 0) or 0
    )


async def _backoff(attempt: int, *, retry_after: str | None = None) -> None:
    """指数退避：0.5s * 2^attempt（可被 Retry-After 覆盖，封顶 4s）。"""
    import asyncio

    if retry_after:
        try:
            delay = min(float(retry_after), 4.0)
        except ValueError:
            delay = min(0.5 * (2**attempt), 4.0)
    else:
        delay = min(0.5 * (2**attempt), 4.0)
    if attempt > 0:
        await asyncio.sleep(delay)


__all__ = [
    "LLMRequestError",
    "OpenAICompatProvider",
    "StructuredOutputError",
]
