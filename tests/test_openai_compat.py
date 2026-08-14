"""OpenAICompatProvider 测试（httpx MockTransport，不访问网络）。

覆盖 09 §3 结构化策略与 V1-c 返工项：
- 严格 Schema（strict + extra=forbid、findings 必填、字符串数字拒绝、未知字段拒绝）
- schema_first → json_object 降级（仅“不支持”400）、修复重试（单次）、fail-soft
- 输出 token 上限（structured 首次与修复请求）
- complete(schema) 真正使用 schema
- client 生命周期（自建关闭 / 注入不关闭）、backoff 无尾等待
- 200 响应结构校验（非 JSON / 空 choices / content 类型异常）
"""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from reposage.providers.llm import smoke as smoke_mod
from reposage.providers.llm.openai_compat import (
    LLMRequestError,
    OpenAICompatProvider,
    StructuredOutputError,
)
from reposage.providers.llm.schema import (
    StrictFindingsEnvelope,
    envelope_json_schema,
    envelope_to_candidates,
    normalize_enums,
    parse_envelope,
)

# 合法信封（两条 finding + summary；严格 Schema 可接受）
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


def _provider_owned() -> OpenAICompatProvider:
    """自建 client 的 provider（仅测生命周期，不发请求）。"""
    return OpenAICompatProvider(model="m", api_key="k", base_url="https://example.invalid/v1")


# ================= 严格 Schema（P1-1 / P1-2） =================


def test_normalize_enums_lowercases():
    out = normalize_enums({"severity": " HIGH ", "category": "SECURITY"})
    assert out["severity"] == "high"
    assert out["category"] == "security"


def test_normalize_enums_keeps_invalid():
    out = normalize_enums({"severity": "blocker", "category": "security"})
    assert out["severity"] == "blocker"  # 非法值原样保留，由校验拒绝


def test_parse_envelope_valid():
    env = parse_envelope(json.dumps(VALID_ENVELOPE))
    assert isinstance(env, StrictFindingsEnvelope)
    assert len(env.findings) == 2
    assert env.findings[0].severity == "high"
    assert env.findings[0].claimed_path == "src/app.py"
    assert env.summary == {"count": 2}
    # 显式转换为领域 FindingCandidate
    candidates = envelope_to_candidates(env)
    assert candidates[0].severity.value == "high"
    assert candidates[0].claimed_start_line == 3


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


# --- P1-2：findings 必填 ---


@pytest.mark.parametrize("payload", ["{}", '{"summary": {}}', '{"findings": null}'])
def test_missing_findings_rejected(payload):
    """缺失/为 null 的 findings 一律拒绝（避免误判为“零问题”）。"""
    with pytest.raises(ValueError):
        parse_envelope(payload)


# --- P1-1：strict + extra=forbid ---


def test_envelope_unknown_field_rejected():
    bad = _copy_envelope()
    bad["unexpected_envelope_field"] = True
    with pytest.raises(ValidationError):
        parse_envelope(json.dumps(bad))


def test_finding_unknown_field_rejected():
    bad = _copy_envelope()
    bad["findings"][0]["unknown_field"] = "silently accepted"
    with pytest.raises(ValidationError):
        parse_envelope(json.dumps(bad))


def test_confidence_string_not_coerced():
    """字符串 "0.9" 不得隐式转换为 float（strict）。"""
    bad = _copy_envelope()
    bad["findings"][0]["confidence"] = "0.9"
    with pytest.raises(ValidationError):
        parse_envelope(json.dumps(bad))


def test_claimed_line_string_not_coerced():
    """字符串 "3" 不得隐式转换为 int（strict）。"""
    bad = _copy_envelope()
    bad["findings"][0]["claimed_start_line"] = "3"
    with pytest.raises(ValidationError):
        parse_envelope(json.dumps(bad))


def test_envelope_json_schema_has_required_findings():
    schema = envelope_json_schema()
    assert schema["type"] == "object"
    assert "findings" in schema["properties"]
    assert "findings" in schema.get("required", [])  # 必填约束进入端点 Schema


# ================= structured：成功路径 =================


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
    # P1-1 测试 6：schema 包含严格约束（required findings）
    schema = calls[0]["response_format"]["json_schema"]["schema"]
    assert "findings" in schema["required"]
    # P1-3 测试 7：输出上限
    assert calls[0]["max_tokens"] == provider.max_output_tokens


@pytest.mark.asyncio
async def test_structured_repair_usage_accumulated():
    """修复重试时两次 usage 都计入（成本不低于真实账单）。"""
    states = iter(
        [
            _ok_response("not json at all", {"prompt_tokens": 10, "completion_tokens": 5}),
            _ok_response(json.dumps(VALID_ENVELOPE), {"prompt_tokens": 20, "completion_tokens": 8}),
        ]
    )
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return next(states)

    provider = _provider(handler)
    findings, usage = await provider.structured([{"role": "user", "content": "review"}])
    assert len(findings) == 2
    assert usage.input_tokens == 30  # 10 + 20
    assert usage.output_tokens == 13  # 5 + 8
    # 修复请求也带输出上限（P1-3 测试 7）
    assert requests[1]["max_tokens"] == provider.max_output_tokens
    assert provider.stats["repairs"] == 1


# ================= structured：修复重试与 fail-soft =================


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


# ================= structured：schema_first 降级 =================


@pytest.mark.asyncio
async def test_structured_schema_first_fallback_to_json_object():
    """端点明确表示不支持 json_schema → 降级 json_object 后成功。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(
                400,
                json={"error": {"message": "Unsupported 'json_schema' parameter"}},
            )
        assert body["response_format"] == {"type": "json_object"}
        return _ok_response(json.dumps(VALID_ENVELOPE))

    provider = _provider(handler)
    findings, _ = await provider.structured([{"role": "user", "content": "review"}])
    assert len(findings) == 2
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_structured_other_400_not_fallback():
    """非“不支持 schema”的 400（如模型名错误）不降级，直接失败且只调 1 次（P2-2）。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "Model not found: DP-V4-PRO"}})

    provider = _provider(handler)
    with pytest.raises(LLMRequestError) as exc:
        await provider.structured([{"role": "user", "content": "review"}])
    assert exc.value.status_code == 400
    assert calls == 1  # 未降级


@pytest.mark.asyncio
async def test_structured_json_repair_strategy_no_response_format():
    """structured_strategy=json_repair：请求不带 response_format，但仍带输出上限。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return _ok_response(json.dumps(VALID_ENVELOPE))

    provider = _provider(handler)
    provider.structured_strategy = "json_repair"
    findings, _ = await provider.structured([{"role": "user", "content": "review"}])
    assert len(findings) == 2
    assert "response_format" not in calls[0]
    assert calls[0]["max_tokens"] == provider.max_output_tokens


# ================= 网络重试 =================


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
    assert provider.stats["http_429"] == 1
    assert provider.stats["http_retries"] == 1


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


# --- P2-4：退避时序 ---


@pytest.mark.asyncio
async def test_backoff_no_sleep_after_last_attempt():
    """最后一次尝试失败后不再等待（P2-4：max_retries=2 → 只 sleep 前 2 次）。"""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    provider = _provider(handler)
    provider._sleep = fake_sleep
    with pytest.raises(LLMRequestError):
        await provider.complete([{"role": "user", "content": "hi"}])
    assert len(sleeps) == 2  # attempt 0、1 等待；attempt 2（最后一次）不再等待
    assert provider.stats["http_retries"] == 3


# ================= 200 响应结构校验（P2-5） =================


@pytest.mark.asyncio
async def test_malformed_200_json_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"this is not json")

    provider = _provider(handler)
    with pytest.raises(LLMRequestError):
        await provider.complete([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_empty_choices_fails_explicitly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    provider = _provider(handler)
    with pytest.raises(LLMRequestError):
        await provider.complete([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_non_string_content_fails_explicitly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": 123}}]})

    provider = _provider(handler)
    with pytest.raises(LLMRequestError):
        await provider.complete([{"role": "user", "content": "hi"}])


# ================= complete（P2-1） =================


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
async def test_complete_with_schema_sends_and_parses_schema():
    """complete(schema) 真正发送传入的 schema 并把解析结果写入 data（P2-1）。"""
    my_schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["schema"] == my_schema
        return _ok_response('{"ok": true}')

    provider = _provider(handler)
    resp = await provider.complete([{"role": "user", "content": "hi"}], schema=my_schema)
    assert calls[0]["response_format"]["json_schema"]["schema"] == my_schema
    assert resp.data == {"ok": True}  # 解析后的 JSON 写入 data


@pytest.mark.asyncio
async def test_complete_max_tokens_passed():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["max_tokens"] == 500
        return _ok_response("OK")

    provider = _provider(handler)
    await provider.complete([{"role": "user", "content": "hi"}], max_tokens=500)


# ================= client 生命周期（P2-3） =================


@pytest.mark.asyncio
async def test_owned_client_closed():
    provider = _provider_owned()
    assert provider._owns_client is True
    client = provider._client
    assert not client.is_closed
    await provider.aclose()
    assert client.is_closed


@pytest.mark.asyncio
async def test_injected_client_not_closed():
    transport = httpx.MockTransport(lambda r: _ok_response("{}"))
    client = httpx.AsyncClient(base_url="https://example.invalid/v1", transport=transport)
    provider = OpenAICompatProvider(
        model="m", api_key="k", base_url="https://example.invalid/v1", http_client=client
    )
    assert provider._owns_client is False
    await provider.aclose()
    assert not client.is_closed  # 外部注入 client 不被关闭
    await client.aclose()


@pytest.mark.asyncio
async def test_async_context_manager_closes_owned_client():
    async with OpenAICompatProvider(
        model="m", api_key="k", base_url="https://example.invalid/v1"
    ) as provider:
        assert not provider._client.is_closed
    assert provider._client.is_closed


# ================= 构造与配置 =================


def test_provider_requires_api_key():
    with pytest.raises(ValueError):
        OpenAICompatProvider(model="m", api_key="", base_url="https://x/v1")


def test_provider_requires_base_url():
    with pytest.raises(ValueError):
        OpenAICompatProvider(model="m", api_key="k", base_url="")


def test_tool_loop_not_implemented_yet():
    provider = _provider(lambda r: _ok_response("{}"))
    with pytest.raises(NotImplementedError):
        asyncio.run(provider.tool_loop(object(), [], budget={}))


# ================= smoke（P1-4：20 轮 / 并发 / 脱敏） =================


def test_smoke_default_rounds_is_20():
    """默认 20 轮（架构要求重复 20 次测稳定 JSON，P1-4）。"""
    assert smoke_mod.DEFAULT_ROUNDS == 20


def test_smoke_concurrency_constant():
    """smoke 并发 3 请求。"""
    assert smoke_mod.CONCURRENCY == 3


def test_smoke_redact_base_url_strips_credentials():
    redacted = smoke_mod._redact_base_url("https://user:pass@host.example/v1?token=secret")
    assert "pass" not in redacted
    assert "secret" not in redacted
    assert redacted == "https://host.example/v1"


def test_smoke_report_never_contains_key_field():
    """报告结构不包含 api key / Authorization 字段（P1-4 脱敏）。"""
    from pathlib import Path

    src = Path(smoke_mod.__file__).read_text(encoding="utf-8")
    assert '"model":' in src
    assert '"failures":' in src
    assert '"api_key"' not in src
    assert "authorization" not in src.lower() or "_redact" in src
