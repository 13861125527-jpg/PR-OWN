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
from pydantic import BaseModel, ConfigDict, ValidationError
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
    # P2-3：attempts=3、retryable=3、实际 retries=2（末次失败不再重试）
    assert provider.stats["http_attempts"] == 3
    assert provider.stats["http_retryable_failures"] == 3
    assert provider.stats["http_retries"] == 2


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
async def test_complete_with_schema_sends_and_validates():
    """complete(schema=Pydantic 模型) 真正发送模型 schema 并本地校验写入 data（P2-1）。"""
    class _OkModel(BaseModel):
        model_config = ConfigDict(extra="forbid")

        ok: bool

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["schema"] == _OkModel.model_json_schema()
        return _ok_response('{"ok": true}')

    provider = _provider(handler)
    resp = await provider.complete([{"role": "user", "content": "hi"}], schema=_OkModel)
    assert calls[0]["response_format"]["json_schema"]["schema"] == _OkModel.model_json_schema()
    assert resp.data == {"ok": True}  # 本地校验通过后的 model_dump


@pytest.mark.asyncio
async def test_complete_schema_required_missing_rejected():
    """P2-1：required 缺失被拒绝（不写入 data）。"""
    class _Req(BaseModel):
        ok: bool

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response('{"wrong": 123}')

    provider = _provider(handler)
    with pytest.raises(StructuredOutputError):
        await provider.complete([{"role": "user", "content": "hi"}], schema=_Req)


@pytest.mark.asyncio
async def test_complete_schema_type_error_rejected():
    """P2-1：属性类型错误被拒绝。"""
    class _Typed(BaseModel):
        model_config = ConfigDict(strict=True)  # 字符串不得隐式转 bool

        ok: bool

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response('{"ok": "yes"}')

    provider = _provider(handler)
    with pytest.raises(StructuredOutputError):
        await provider.complete([{"role": "user", "content": "hi"}], schema=_Typed)


@pytest.mark.asyncio
async def test_complete_schema_additional_properties_rejected():
    """P2-1：additionalProperties 违规（未知字段）被拒绝。"""
    class _Forbid(BaseModel):
        model_config = ConfigDict(extra="forbid")

        ok: bool

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response('{"ok": true, "unexpected": 1}')

    provider = _provider(handler)
    with pytest.raises(StructuredOutputError):
        await provider.complete([{"role": "user", "content": "hi"}], schema=_Forbid)


@pytest.mark.asyncio
async def test_complete_default_max_tokens():
    """P2-2：complete 默认调用也带输出上限（max_output_tokens）。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return _ok_response("OK")

    provider = _provider(handler)
    await provider.complete([{"role": "user", "content": "hi"}])  # 不传 max_tokens
    assert calls[0]["max_tokens"] == provider.max_output_tokens


@pytest.mark.asyncio
async def test_retry_stats_attempts_vs_retries():
    """P2-3：http_attempts 与 http_retries 语义区分（全 503）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    provider = _provider(handler)  # max_retries=2
    with pytest.raises(LLMRequestError):
        await provider.complete([{"role": "user", "content": "hi"}])
    assert provider.stats["http_attempts"] == 3
    assert provider.stats["http_retryable_failures"] == 3
    assert provider.stats["http_retries"] == 2  # 真正额外尝试只有 2 次


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
# ================= V1-c 第二轮返工：confidence 范围 / 修复边界 / 400 判断 / smoke 控制流 =================


@pytest.mark.parametrize("bad_conf", [-0.1, 1.1, 2.5])
def test_confidence_out_of_range_rejected(bad_conf):
    """P1-1：confidence 超出 0-1 被严格 Schema 拒绝。"""
    bad = _copy_envelope()
    bad["findings"][0]["confidence"] = bad_conf
    with pytest.raises(ValidationError):
        parse_envelope(json.dumps(bad))


def test_json_schema_confidence_min_max():
    """P1-1：JSON Schema 中 confidence 有 minimum/maximum。"""
    schema = envelope_json_schema()
    items = schema["properties"]["findings"]["items"]
    if "$ref" in items:  # pydantic 可能用 $defs 引用
        ref_name = items["$ref"].rsplit("/", 1)[-1]
        conf = schema["$defs"][ref_name]["properties"]["confidence"]
    else:
        conf = items["properties"]["confidence"]
    assert conf.get("minimum") == 0.0
    assert conf.get("maximum") == 1.0


@pytest.mark.asyncio
async def test_structured_conversion_error_enters_repair():
    """P1-1：strict→domain 转换异常进入同一修复边界（首次失败→修复→成功）。"""
    import reposage.providers.llm.openai_compat as oc

    real_convert = oc.envelope_to_candidates
    calls = 0

    def flaky_convert(envelope):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValidationError.from_exception_data("FindingCandidate", [])
        return real_convert(envelope)

    states = iter([_ok_response(json.dumps(VALID_ENVELOPE))] * 2)

    def handler(request: httpx.Request) -> httpx.Response:
        return next(states)

    provider = _provider(handler)
    oc.envelope_to_candidates = flaky_convert  # type: ignore[assignment]
    try:
        findings, _ = await provider.structured([{"role": "user", "content": "review"}])
    finally:
        oc.envelope_to_candidates = real_convert
    assert len(findings) == 2
    assert calls == 2  # 首次转换失败 → 修复重试 → 第二次成功
    assert provider.stats["repairs"] == 1


@pytest.mark.asyncio
async def test_400_invalid_schema_not_fallback():
    """P2-5：schema 内容非法（仅含 response_format 关键词、无“不支持”语义）不得降级。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            400, json={"error": {"message": "Invalid schema for response_format: confidence missing"}}
        )

    provider = _provider(handler)
    with pytest.raises(LLMRequestError):
        await provider.structured([{"role": "user", "content": "review"}])
    assert calls == 1  # 未降级


# ---- smoke 控制流（P2-4） ----


class _FakeSmokeProvider:
    """可配置假 provider：控制 minimal/concurrency/structured 成功与否。"""

    def __init__(self, *, minimal_ok=True, concurrency_ok=True, structured_ok=True) -> None:
        self.stats = {
            "repairs": 0,
            "http_attempts": 0,
            "http_retryable_failures": 0,
            "http_retries": 0,
            "http_429": 0,
            "schema_fallbacks": 0,
            "response_format_fallbacks": 0,
        }
        self._minimal_ok = minimal_ok
        self._concurrency_ok = concurrency_ok
        self._structured_ok = structured_ok
        self._complete_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def complete(self, messages, *, temperature=0.0, **kwargs):
        from reposage.domain.models import ModelUsage
        from reposage.domain.protocols import ModelResponse

        self._complete_calls += 1
        if not self._minimal_ok and self._complete_calls == 1:
            raise LLMRequestError("minimal boom")
        if not self._concurrency_ok and self._complete_calls > 1:
            raise LLMRequestError("concurrency boom")
        return ModelResponse(text="OK", usage=ModelUsage(model="fake", role="smoke"))

    async def structured(self, messages):
        from reposage.domain.models import ModelUsage

        if not self._structured_ok:
            raise StructuredOutputError("structure boom", detail="boom")
        return [], ModelUsage(model="fake", role="smoke")


@pytest.mark.asyncio
async def test_smoke_minimal_failure_still_writes_report(tmp_path, monkeypatch):
    """P2-4：最小请求失败仍落盘脱敏报告，且整体 passed=False。"""
    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setenv("MODEL_BASE_URL", "https://example.invalid/v1")
    report_path = tmp_path / "smoke.json"
    factory = lambda cfg: _FakeSmokeProvider(minimal_ok=False)  # noqa: E731
    code = await smoke_mod._run(rounds=3, report_path=str(report_path), provider_factory=factory)
    assert code == 1
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["passed"] is False
    assert data["checks"]["minimal_request"]["ok"] is False


@pytest.mark.asyncio
async def test_smoke_concurrency_failure_exit_1(tmp_path, monkeypatch):
    """P2-4：并发失败（structured 正常）仍整体 exit 1。"""
    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setenv("MODEL_BASE_URL", "https://example.invalid/v1")
    report_path = tmp_path / "smoke.json"
    factory = lambda cfg: _FakeSmokeProvider(concurrency_ok=False)  # noqa: E731
    code = await smoke_mod._run(rounds=3, report_path=str(report_path), provider_factory=factory)
    assert code == 1
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["checks"]["concurrency_3"]["ok"] is False
    assert data["checks"]["structured"]["ok"] is True
    assert data["passed"] is False


@pytest.mark.asyncio
async def test_smoke_all_pass_passed_true(tmp_path, monkeypatch):
    """P2-4：全部 mandatory 通过 → exit 0 且报告 passed=True。"""
    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setenv("MODEL_BASE_URL", "https://example.invalid/v1")
    report_path = tmp_path / "smoke.json"
    factory = lambda cfg: _FakeSmokeProvider()  # noqa: E731
    code = await smoke_mod._run(rounds=3, report_path=str(report_path), provider_factory=factory)
    assert code == 0
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["passed"] is True
    assert data["checks"]["minimal_request"]["ok"] is True
    assert data["checks"]["concurrency_3"]["ok"] is True
    assert data["checks"]["structured"]["ok"] is True
# ================= Round 1 复验：MODEL_NAME 覆盖 =================


class _CaptureFactory:
    """捕获实际传入 factory 的 llm 配置（断言模型名）。"""

    def __init__(self) -> None:
        self.model: str | None = None
        self.calls = 0

    def __call__(self, llm):
        self.calls += 1
        self.model = llm.model
        return _FakeSmokeProvider()


@pytest.mark.asyncio
async def test_smoke_default_model_when_no_env(monkeypatch):
    """未设置 MODEL_NAME → 使用配置 llm.model 默认值。"""
    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setenv("MODEL_BASE_URL", "https://example.invalid/v1")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    capture = _CaptureFactory()
    code = await smoke_mod._run(rounds=1, report_path=None, provider_factory=capture)
    assert code == 0
    assert capture.model == "DP-V4-PRO"  # settings.llm.model 默认值


@pytest.mark.asyncio
async def test_smoke_model_name_env_overrides(monkeypatch):
    """设置 MODEL_NAME → 覆盖配置默认值（不硬编码具体服务商）。"""
    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setenv("MODEL_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("MODEL_NAME", "deepseek-v4-pro")
    capture = _CaptureFactory()
    code = await smoke_mod._run(rounds=1, report_path=None, provider_factory=capture)
    assert code == 0
    assert capture.model == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_smoke_report_model_matches_request(monkeypatch, tmp_path):
    """报告顶层 model 与实际请求模型一致。"""
    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setenv("MODEL_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("MODEL_NAME", "deepseek-v4-flash")
    report_path = tmp_path / "smoke.json"
    capture = _CaptureFactory()
    await smoke_mod._run(rounds=1, report_path=str(report_path), provider_factory=capture)
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["model"] == capture.model == "deepseek-v4-flash"
# ================= Round 2 复验：三级降级（unavailable） =================

_UNAVAILABLE = {"error": {"message": "This response_format type is unavailable now"}}


@pytest.mark.asyncio
async def test_structured_unavailable_jsonschema_fallback_to_json_object():
    """P1-测试 1/2：真实服务 "unavailable" 语义 → json_schema 降级 json_object 后成功（2 次请求）。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json=_UNAVAILABLE)
        assert body["response_format"] == {"type": "json_object"}
        return _ok_response(json.dumps(VALID_ENVELOPE))

    provider = _provider(handler)
    findings, _ = await provider.structured([{"role": "user", "content": "review"}])
    assert len(findings) == 2
    assert len(calls) == 2
    assert provider.stats["schema_fallbacks"] == 1
    assert provider.stats["response_format_fallbacks"] == 0


@pytest.mark.asyncio
async def test_structured_full_three_level_fallback():
    """P1-测试 3：json_schema 与 json_object 均 unavailable → 第三次不携带 response_format 成功。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        fmt = body.get("response_format")
        if fmt is not None:
            return httpx.Response(400, json=_UNAVAILABLE)  # 两种 response_format 都不可用
        assert "response_format" not in body
        return _ok_response(json.dumps(VALID_ENVELOPE))

    provider = _provider(handler)
    findings, _ = await provider.structured([{"role": "user", "content": "review"}])
    assert len(findings) == 2
    assert len(calls) == 3
    assert "response_format" not in calls[2]
    assert provider.stats["schema_fallbacks"] == 1
    assert provider.stats["response_format_fallbacks"] == 1


@pytest.mark.asyncio
async def test_structured_fallback_then_repair_strict_validation():
    """P1-测试 6：降级成功后输出坏 JSON → 修复重试（plain）仍走严格校验并成功。"""
    states = iter(
        [
            httpx.Response(400, json=_UNAVAILABLE),  # json_schema 不支持
            _ok_response("not json at all"),  # json_object 输出坏内容
            _ok_response(json.dumps(VALID_ENVELOPE)),  # 修复请求（plain）成功
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(states)

    provider = _provider(handler)
    findings, _ = await provider.structured([{"role": "user", "content": "review"}])
    assert len(findings) == 2
    assert provider.stats["schema_fallbacks"] == 1
    assert provider.stats["repairs"] == 1


@pytest.mark.asyncio
async def test_fallback_attempts_not_counted_as_retries():
    """P1-测试 7：降级请求计入 http_attempts，不误计为网络重试 http_retries。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json=_UNAVAILABLE)
        return _ok_response(json.dumps(VALID_ENVELOPE))

    provider = _provider(handler)
    await provider.structured([{"role": "user", "content": "review"}])
    assert calls == 2
    assert provider.stats["http_attempts"] == 2  # 两次 HTTP 尝试都计入
    assert provider.stats["http_retries"] == 0  # 400 非 429/5xx，不算网络重试
    assert provider.stats["http_retryable_failures"] == 0


@pytest.mark.asyncio
async def test_invalid_schema_400_still_no_fallback():
    """P1-测试 4：Schema 内容非法（无“不支持”语义）仍不降级（含 unavailable 场景边界）。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            400, json={"error": {"message": "Invalid schema for response_format: confidence missing"}}
        )

    provider = _provider(handler)
    with pytest.raises(LLMRequestError):
        await provider.structured([{"role": "user", "content": "review"}])
    assert calls == 1


@pytest.mark.asyncio
async def test_non_400_and_auth_errors_no_fallback():
    """P1-测试 5：非 400、认证/模型名错误不降级。"""
    for status, message in [
        (401, "Invalid API key"),
        (404, "Model not found"),
        (500, "internal unavailable"),  # 5xx 走网络重试，不走能力降级
    ]:
        calls = 0

        def handler(request: httpx.Request, _status: int = status, _msg: str = message) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(_status, json={"error": {"message": _msg}})

        provider = _provider(handler)
        with pytest.raises(LLMRequestError):
            await provider.structured([{"role": "user", "content": "review"}])
        # 401/404：不降级也不重试（1 次）；500：网络重试（max_retries+1=3 次）
        expected = 1 if status < 500 else 3
        assert calls == expected, f"{status}: 期望 {expected} 次请求，实际 {calls}"
        assert provider.stats["schema_fallbacks"] == 0
        assert provider.stats["response_format_fallbacks"] == 0
@pytest.mark.asyncio
async def test_complete_schema_fallback_counts_and_succeeds():
    """review 建议：complete(schema) json_schema unavailable → 降级 json_object 并计入 schema_fallbacks。"""
    class _OkModel(BaseModel):
        ok: bool

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json=_UNAVAILABLE)
        assert body["response_format"] == {"type": "json_object"}
        return _ok_response('{"ok": true}')

    provider = _provider(handler)
    resp = await provider.complete([{"role": "user", "content": "hi"}], schema=_OkModel)
    assert resp.data == {"ok": True}
    assert len(calls) == 2
    assert provider.stats["schema_fallbacks"] == 1
