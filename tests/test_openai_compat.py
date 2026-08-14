"""OpenAICompatProvider 测试（httpx MockTransport，不访问网络）。

覆盖 09 §3 结构化策略：schema_first → json_object 降级、修复重试（单次）、
fail-soft 抛错、网络重试（429/5xx）、usage 记账。
"""

import json

import httpx
import pytest
from pydantic import ValidationError
from reposage.providers.llm.openai_compat import (
    LLMRequestError,
    OpenAICompatProvider,
    StructuredOutputError,
)
from reposage.providers.llm.schema import envelope_json_schema, normalize_enums, parse_envelope

# 合法信封（两条 finding + summary）
VALID_ENVELOPE = {
    "findings": [
        {
            "title": "eval 动态代码执行",
            "severity": "high",
            "confidence": 0.9,
            "category": "security",
            "claimed_path": "src/app.py",
            "claimed_start_line": 3,
            "claimed_end_line": 3,
            "trigger_condition": "eval(data)",
            "impact": "任意代码执行",
            "explanation": "eval 处理不可信输入",
            "suggestion": "改用白名单解析",
        },
        {
            "title": "生产 assert",
            "severity": "medium",
            "confidence": 0.7,
            "category": "correctness",
            "claimed_path": "src/app.py",
            "claimed_start_line": 5,
        },
    ],
    "summary": {"count": 2},
}


def _ok_response(content: str, usage: dict | None = None) -> httpx.Response:
    payload = {
        "choices": [{"message": {"content": content, "role": "assistant"}}],
        "usage": usage or {"prompt_tokens": 120, "completion_tokens": 45},
    }
    return httpx.Response(200, json=payload)


def _copy_envelope() -> dict:
    """深拷贝共享常量，避免测试间浅拷贝污染。"""
    import copy

    return copy.deepcopy(VALID_ENVELOPE)


def _provider(handler) -> OpenAICompatProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://example.invalid/v1", transport=transport)
    return OpenAICompatProvider(
        model="DP-V4-PRO",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        http_client=client,
        max_retries=2,
    )


# ---- schema 单元 ----

VALID_FINDING = VALID_ENVELOPE["findings"][0]


def test_normalize_enums_lowercases():
    out = normalize_enums({"severity": " HIGH ", "category": "SECURITY"})
    assert out["severity"] == "high"
    assert out["category"] == "security"


def test_normalize_enums_keeps_invalid():
    out = normalize_enums({"severity": "blocker", "category": "security"})
    assert out["severity"] == "blocker"  # 非法值原样保留，由校验拒绝


def test_parse_envelope_valid():
    env = parse_envelope(json.dumps(VALID_ENVELOPE))
    assert len(env.findings) == 2
    assert env.findings[0].severity.value == "high"
    assert env.findings[0].claimed_path == "src/app.py"
    assert env.summary == {"count": 2}


def test_parse_envelope_invalid_json():
    with pytest.raises(json.JSONDecodeError):
        parse_envelope("not json")


def test_parse_envelope_wrong_shape():
    with pytest.raises(ValueError):
        parse_envelope(json.dumps(["a", "b"]))
    with pytest.raises(ValueError):
        parse_envelope(json.dumps({"findings": "not-a-list"}))


def test_parse_envelope_validation_error():
    bad = _copy_envelope()
    bad["findings"][0]["severity"] = "blocker"  # 不在白名单
    with pytest.raises(ValidationError):
        parse_envelope(json.dumps(bad))


def test_envelope_json_schema_shape():
    schema = envelope_json_schema()
    assert schema["type"] == "object"
    assert "findings" in schema["properties"]
    assert "summary" in schema["properties"]


# ---- structured：成功路径 ----

@pytest.mark.asyncio
async def test_structured_success():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return _ok_response(json.dumps(VALID_ENVELOPE))

    provider = _provider(handler)
    findings, usage = await provider.structured([{"role": "user", "content": "review"}])
    assert len(findings) == 2
    assert usage.input_tokens == 120
    assert usage.output_tokens == 45
    assert usage.model == "DP-V4-PRO"
    # schema_first：请求带 response_format json_schema
    assert calls[0]["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_structured_repair_usage_accumulated():
    """修复重试时两次 usage 都计入（成本不低于真实账单）。"""
    states = iter(
        [
            _ok_response("not json at all", {"prompt_tokens": 10, "completion_tokens": 5}),
            _ok_response(json.dumps(VALID_ENVELOPE), {"prompt_tokens": 20, "completion_tokens": 8}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(states)

    provider = _provider(handler)
    findings, usage = await provider.structured([{"role": "user", "content": "review"}])
    assert len(findings) == 2
    assert usage.input_tokens == 30  # 10 + 20
    assert usage.output_tokens == 13  # 5 + 8


# ---- structured：修复重试与 fail-soft ----

@pytest.mark.asyncio
async def test_structured_repair_on_validation_error():
    """第一次枚举非法 → 修复重试（错误回喂）→ 第二次成功。"""
    bad = _copy_envelope()
    bad["findings"][0]["severity"] = "blocker"
    states = iter(
        [
            _ok_response(json.dumps(bad)),
            _ok_response(json.dumps(VALID_ENVELOPE)),
        ]
    )
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return next(states)

    provider = _provider(handler)
    findings, _ = await provider.structured([{"role": "user", "content": "review"}])
    assert len(findings) == 2
    assert len(requests) == 2
    # 修复请求携带错误回喂
    repair_messages = requests[1]["messages"]
    assert any("无法解析" in m.get("content", "") for m in repair_messages if m["role"] == "user")


@pytest.mark.asyncio
async def test_structured_repair_fails_raises():
    """修复重试仍失败 → StructuredOutputError（调用方 fail-soft）。"""
    states = iter(
        [
            _ok_response("garbage"),
            _ok_response("garbage again"),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(states)

    provider = _provider(handler)
    with pytest.raises(StructuredOutputError):
        await provider.structured([{"role": "user", "content": "review"}])


# ---- structured：schema_first 降级 ----

@pytest.mark.asyncio
async def test_structured_schema_first_fallback_to_json_object():
    """端点不支持 json_schema（400）→ 降级 json_object 后成功。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": {"message": "json_schema not supported"}})
        assert body["response_format"] == {"type": "json_object"}
        return _ok_response(json.dumps(VALID_ENVELOPE))

    provider = _provider(handler)
    findings, _ = await provider.structured([{"role": "user", "content": "review"}])
    assert len(findings) == 2
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_structured_json_repair_strategy_no_response_format():
    """structured_strategy=json_repair：请求不带 response_format。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return _ok_response(json.dumps(VALID_ENVELOPE))

    provider = _provider(handler)
    provider.structured_strategy = "json_repair"
    findings, _ = await provider.structured([{"role": "user", "content": "review"}])
    assert len(findings) == 2
    assert "response_format" not in calls[0]


# ---- 网络重试 ----

@pytest.mark.asyncio
async def test_http_retry_on_429_then_success():
    responses = iter(
        [
            httpx.Response(429, json={"error": {"message": "rate limited"}}),
            _ok_response(json.dumps(VALID_ENVELOPE)),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    provider = _provider(handler)
    findings, _ = await provider.structured([{"role": "user", "content": "review"}])
    assert len(findings) == 2


@pytest.mark.asyncio
async def test_http_retry_exhausted_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    provider = _provider(handler)  # max_retries=2
    with pytest.raises(LLMRequestError):
        await provider.complete([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_http_4xx_not_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    provider = _provider(handler)
    with pytest.raises(LLMRequestError) as exc:
        await provider.complete([{"role": "user", "content": "hi"}])
    assert exc.value.status_code == 401
    assert calls == 1  # 4xx 非 429 不重试


# ---- complete ----

@pytest.mark.asyncio
async def test_complete_returns_text_and_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response("OK", {"prompt_tokens": 5, "completion_tokens": 3})

    provider = _provider(handler)
    resp = await provider.complete([{"role": "user", "content": "hi"}])
    assert resp.text == "OK"
    assert resp.usage is not None
    assert resp.usage.input_tokens == 5
    assert resp.usage.output_tokens == 3


@pytest.mark.asyncio
async def test_complete_with_schema_sets_json_object():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        return _ok_response("{}")

    provider = _provider(handler)
    await provider.complete([{"role": "user", "content": "hi"}], schema={})


# ---- 构造与配置 ----

def test_provider_requires_api_key():
    with pytest.raises(ValueError):
        OpenAICompatProvider(
            model="m", api_key="", base_url="https://x/v1", http_client=httpx.AsyncClient()
        )


def test_provider_requires_base_url():
    with pytest.raises(ValueError):
        OpenAICompatProvider(
            model="m", api_key="k", base_url="", http_client=httpx.AsyncClient()
        )


def test_tool_loop_not_implemented_yet():
    provider = _provider(lambda r: _ok_response("{}"))
    with pytest.raises(NotImplementedError):
        # 需要 async 上下文
        import asyncio

        asyncio.run(provider.tool_loop(object(), [], budget={}))
