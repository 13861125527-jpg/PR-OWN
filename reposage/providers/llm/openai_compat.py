"""OpenAI-compatible LLM Provider（providers/llm/openai_compat.py，09 §3 / 14 OQ-1）。

只依赖 OpenAI-compatible ``/chat/completions`` 协议；base_url/api_key 从环境读取
（LLMConfig.api_key_env / base_url_env）。

结构化输出策略（09 §3）：
- schema_first：优先 ``response_format={"type": "json_schema"}``；**仅当错误明确表示
  不支持 json_schema/response_format** 时降级 ``{"type": "json_object"}``（其他 400
  直接失败，不掩盖配置错误，P2-2）；
- json_repair：不依赖 response_format，普通输出 + 解析；
- 两种策略统一走：解析 → Pydantic 严格校验（strict + extra=forbid）→ 单次修复
  重试（把校验错误回喂）→ 仍失败抛 StructuredOutputError（调用方 fail-soft 记 Coverage）。

输出预算（P1-3）：structured 首次与修复请求均携带 ``max_tokens``（09 §5：
findings ~2k + summary ~1k）；complete 允许调用方传入输出上限。

可靠性：网络错误/429/5xx 指数退避重试（max_retries，最后一次失败不等待）；
非 429 的 4xx 不重试；200 响应做结构校验（非 JSON / 缺 choices → LLMRequestError）。
成本：usage token 精确记账；cost_usd 未定价标 0（V1-f 成本监控补价格）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from reposage.config.settings import LLMConfig
from reposage.domain.finding import FindingCandidate
from reposage.domain.models import ModelUsage
from reposage.domain.protocols import ModelResponse

from .schema import envelope_json_schema, envelope_to_candidates, parse_envelope

# 结构化系统指令：要求只输出 JSON 信封（json_repair / 降级路径用）
_SYSTEM_JSON_INSTRUCTION = """\
你只输出一个 JSON 对象，不要输出任何其他文本、Markdown 代码块或解释。
信封格式：{"findings": [{"title": str, "severity": "critical|high|medium|low|info", \
"confidence": float(0-1), "category": "correctness|security|silent_failure|concurrency|edge_case|test_gap|performance|maintainability", \
"claimed_path": str|null, "claimed_start_line": int|null, "claimed_end_line": int|null, \
"chunk_id": str|null, "evidence_ref": str|null, "trigger_condition": str, "impact": str, "explanation": str, "suggestion": str, "is_outside_diff": bool}], \
"summary": {}}"""

# 400 错误消息中表示“不支持 json_schema/response_format”的判断（P2-5）
# 需同时满足：不支持语义 + 目标字段，避免宽泛子串误降级
_SCHEMA_UNSUPPORTED_SEMANTICS = ("not support", "unsupported", "unknown parameter")


class LLMRequestError(RuntimeError):
    """网络/HTTP 层失败（重试耗尽、4xx 永久错误或 200 响应结构异常）。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_body = error_body  # 脱敏：仅错误片段（不含请求内容）


class StructuredOutputError(RuntimeError):
    """结构化输出解析/校验失败（修复重试后仍失败；调用方 fail-soft）。"""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class OpenAICompatProvider:
    """OpenAI-compatible 提供者（httpx 异步客户端；自建 client 支持 aclose/async with）。"""

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
        max_output_tokens: int = 3000,
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
        self.max_output_tokens = max_output_tokens
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        self._sleep = asyncio.sleep  # 可注入 sleeper（测试退避时序）
        # 运行时统计（smoke 报告用）：attempts / retryable / 实际 retries / 429 / 修复
        self.stats: dict[str, int] = {
            "http_attempts": 0,  # 所有请求尝试次数
            "http_retryable_failures": 0,  # 可重试失败（429/5xx/网络错误）
            "http_retries": 0,  # 实际执行的额外尝试（末次失败不再重试不计入）
            "http_429": 0,
            "repairs": 0,
        }

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
            raise ValueError(f"缺少 API key：环境变量 {llm.api_key_env} 未设置")
        if not base_url:
            raise ValueError(f"缺少 base_url：环境变量 {llm.base_url_env} 未设置")
        return cls(
            model=llm.model,
            api_key=api_key,
            base_url=base_url,
            temperature=llm.temperature,
            timeout_seconds=llm.timeout_seconds,
            max_retries=llm.max_retries,
            structured_strategy=llm.structured_strategy,
            max_output_tokens=llm.max_output_tokens,
            http_client=http_client,
        )

    # ---- client 生命周期（P2-3：只关闭自建 client） ----

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> OpenAICompatProvider:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---- 通用补全 ----

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: type[BaseModel] | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        """通用补全。

        - schema 为 Pydantic 模型类时：真正发送 ``response_format=json_schema``
          （端点不支持则降级 json_object），并用 ``model_validate_json`` **本地校验**
          响应，非法结果抛 StructuredOutputError 且不写入 data（P2-1）；
        - 输出上限：显式 max_tokens 优先，否则使用 self.max_output_tokens
          （所有生产调用都有上限，P2-2）。
        """
        effective_max_tokens = max_tokens if max_tokens is not None else self.max_output_tokens
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": effective_max_tokens,
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "completion", "schema": schema.model_json_schema()},
            }
        try:
            data, usage = await self._post(body)
        except LLMRequestError as exc:
            if not _is_schema_unsupported(exc):
                raise
            body["response_format"] = {"type": "json_object"}
            data, usage = await self._post(body)
        text = _content_of(data)
        parsed: dict[str, Any] | None = None
        if schema is not None:
            try:
                parsed = schema.model_validate_json(text).model_dump()
            except ValidationError as exc:
                raise StructuredOutputError(
                    "complete 输出未通过调用方 Schema 校验", detail=f"{type(exc).__name__}: {exc}"
                ) from exc
        return ModelResponse(text=text, data=parsed, usage=self._make_usage(usage))

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
            candidates = envelope_to_candidates(envelope)
        except Exception as exc:  # 解析/校验/领域转换全部进入同一修复边界（P1-1）
            detail = f"{type(exc).__name__}: {exc}"
            self.stats["repairs"] += 1
            text2 = await self._chat_structured_plain(
                [*messages, {"role": "user", "content": _REPAIR_INSTRUCTION.format(detail=detail)}],
                total_usage,
            )
            try:
                envelope2 = parse_envelope(text2)
                candidates = envelope_to_candidates(envelope2)
            except Exception as exc2:
                raise StructuredOutputError(
                    "结构化输出解析失败（修复重试后仍失败）",
                    detail=f"{type(exc2).__name__}: {exc2}",
                ) from exc2
        latency_ms = int((time.monotonic() - started) * 1000)
        return candidates, self._make_usage(total_usage, latency_ms=latency_ms)

    async def _chat_structured_schema_first(self, messages: list[dict[str, Any]], usage_agg: dict[str, Any]) -> str:
        """schema_first：json_schema → 仅“不支持”时降级 json_object + 内嵌 schema。"""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [*messages, {"role": "system", "content": _SYSTEM_JSON_INSTRUCTION}],
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "findings_envelope", "schema": envelope_json_schema()},
            },
        }
        try:
            data, usage = await self._post(body)
        except LLMRequestError as exc:
            if not _is_schema_unsupported(exc):
                raise  # 其他 400：直接失败，不掩盖配置错误
            body["response_format"] = {"type": "json_object"}
            data, usage = await self._post(body)
        _accumulate_usage(usage_agg, usage)
        return _content_of(data)

    async def _chat_structured_plain(self, messages: list[dict[str, Any]], usage_agg: dict[str, Any]) -> str:
        """json_repair / 修复重试：普通输出 + 提示内嵌 schema；同样带输出上限。"""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [*messages, {"role": "system", "content": _SYSTEM_JSON_INSTRUCTION}],
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
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
        """POST /chat/completions。

        - 网络错误/429/5xx：指数退避重试；**最后一次尝试失败不再等待**（P2-4）；
        - 非 429 的 4xx：不重试（永久错误）；
        - 200 响应：结构校验（非 JSON / 非对象 → LLMRequestError，P2-5）；
        - stats：http_attempts 计数所有尝试；http_retryable_failures 计数可重试失败；
          http_retries 只计实际额外尝试（末次失败不再重试不计入，P2-3）。
        """
        last_error: LLMRequestError | None = None
        retry_after: str | None = None
        for attempt in range(self.max_retries + 1):
            self.stats["http_attempts"] += 1
            try:
                resp = await self._client.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:
                self.stats["http_retryable_failures"] += 1
                last_error = LLMRequestError(f"网络错误: {type(exc).__name__}: {exc}")
                retry_after = None
            else:
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except json.JSONDecodeError as exc:
                        raise LLMRequestError(
                            f"200 响应不是合法 JSON（前 120 字符: {resp.text[:120]!r}）"
                        ) from exc
                    if not isinstance(data, dict):
                        raise LLMRequestError(f"200 响应不是 JSON 对象: {type(data).__name__}")
                    return data, data.get("usage", {})
                if resp.status_code == 429 or resp.status_code >= 500:
                    if resp.status_code == 429:
                        self.stats["http_429"] += 1
                    self.stats["http_retryable_failures"] += 1
                    last_error = LLMRequestError(
                        f"HTTP {resp.status_code}", status_code=resp.status_code, error_body=resp.text[:500]
                    )
                    retry_after = resp.headers.get("retry-after")
                else:
                    raise LLMRequestError(
                        f"HTTP {resp.status_code}: {resp.text[:200]}",
                        status_code=resp.status_code,
                        error_body=resp.text[:500],
                    )
            if attempt >= self.max_retries:
                break  # 最后一次尝试失败：不再等待（也不计入 http_retries）
            self.stats["http_retries"] += 1  # 实际还会再试
            await self._backoff(attempt, retry_after)
        raise LLMRequestError(f"重试耗尽（{self.max_retries} 次）: {last_error}")

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        """指数退避：0.5s * 2^attempt（Retry-After 可覆盖，封顶 4s）。"""
        if retry_after:
            try:
                delay = min(float(retry_after), 4.0)
            except ValueError:
                delay = min(0.5 * (2**attempt), 4.0)
        else:
            delay = min(0.5 * (2**attempt), 4.0)
        if delay > 0:
            await self._sleep(delay)

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
    """提取 choices[0].message.content；结构异常抛 LLMRequestError（P2-5）。"""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMRequestError("200 响应缺少 choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise LLMRequestError("choices[0] 缺少 message 对象")
    content = message.get("content")
    if not isinstance(content, str):
        raise LLMRequestError(f"message.content 不是字符串: {type(content).__name__}")
    return content


def _is_schema_unsupported(exc: LLMRequestError) -> bool:
    """400 是否明确表示不支持 json_schema/response_format（P2-5 双条件）。

    需同时满足：不支持语义（not support/unsupported/unknown parameter）+ 目标字段
    （json_schema/response_format）；仅出现目标字段（如 schema 内容非法）不降级。
    """
    if exc.status_code != 400:
        return False
    body = (exc.error_body or "").lower()
    has_semantics = any(hint in body for hint in _SCHEMA_UNSUPPORTED_SEMANTICS)
    has_target = "json_schema" in body or "response_format" in body
    return has_semantics and has_target


def _accumulate_usage(agg: dict[str, Any], usage: dict[str, Any]) -> None:
    agg["prompt_tokens"] = int(agg.get("prompt_tokens", 0)) + int(usage.get("prompt_tokens", 0) or 0)
    agg["completion_tokens"] = int(agg.get("completion_tokens", 0)) + int(
        usage.get("completion_tokens", 0) or 0
    )


__all__ = [
    "LLMRequestError",
    "OpenAICompatProvider",
    "StructuredOutputError",
]
