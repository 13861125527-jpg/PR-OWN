"""结构化输出信封与 JSON Schema（providers/llm/schema.py，09 §3）。

模型输出统一为 ``{"findings": [...], "summary": {...}}`` 信封；Pydantic 严格模式校验；
severity/category 枚举按白名单做大小写归一（枚举白名单映射，09 §3）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from reposage.domain.enums import FindingCategory, Severity
from reposage.domain.finding import FindingCandidate

# 枚举白名单（小写，与 enums 值一致；模型输出统一归一后再校验）
_SEVERITY_VALUES = {s.value for s in Severity}
_CATEGORY_VALUES = {c.value for c in FindingCategory}


class FindingsEnvelope(BaseModel):
    """模型输出的 JSON 信封（09 §3）：findings 列表 + summary 占位。"""

    findings: list[FindingCandidate] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


def envelope_json_schema() -> dict[str, Any]:
    """OpenAI json_schema 响应格式用的 JSON Schema（schema_first 策略）。"""
    return FindingsEnvelope.model_json_schema()


def normalize_enums(record: dict[str, Any]) -> dict[str, Any]:
    """枚举白名单映射：把 severity/category 归一为小写字符串。

    模型可能输出 "HIGH"/"High"/" high "，统一转小写后交给 Pydantic 严格校验；
    不在白名单的值保持原样，由校验阶段拒绝（触发修复重试）。
    """
    out = dict(record)
    for key, allowed in (("severity", _SEVERITY_VALUES), ("category", _CATEGORY_VALUES)):
        value = out.get(key)
        if isinstance(value, str):
            lowered = value.strip().lower()
            out[key] = lowered if lowered in allowed else value  # 非法值原样保留以便报错
    return out


def parse_envelope(text: str) -> FindingsEnvelope:
    """解析模型输出文本为信封。

    解析失败（非 JSON / 字段缺失 / 枚举非法）抛 json.JSONDecodeError /
    pydantic.ValidationError，由调用方执行修复重试（09 §3：单次修复重试 → 失败丢弃该块）。
    """
    import json

    payload = json.loads(text)  # JSONDecodeError 向上抛
    if not isinstance(payload, dict):
        raise ValueError(f"信封必须是对象，得到 {type(payload).__name__}")
    findings = payload.get("findings", [])
    if not isinstance(findings, list):
        raise ValueError("findings 必须是数组")
    normalized = [normalize_enums(item) for item in findings]
    return FindingsEnvelope.model_validate({**payload, "findings": normalized})


__all__ = [
    "FindingsEnvelope",
    "envelope_json_schema",
    "normalize_enums",
    "parse_envelope",
]
