"""结构化输出信封与严格 Schema（providers/llm/schema.py，09 §3）。

模型输出统一为 ``{"findings": [...], "summary": {...}}`` 信封。

严格性（P1-1/P1-2 返工）：
- 模型输出专用 ``StrictFindingCandidateOutput`` / ``StrictFindingsEnvelope``：
  ``ConfigDict(strict=True, extra="forbid")`` —— 禁止字符串数字隐式转换、禁止未知
  字段静默进入、禁止类型强制；
- ``findings`` 必须显式存在（缺字段/为 null 一律拒绝并触发修复重试，避免把字段
  丢失误判为“零问题”）；
- severity/category 枚举白名单小写归一保留（normalize_enums），归一后其余字段
  严格校验；
- 校验通过后**显式转换**为领域 ``FindingCandidate``（不改变 domain 内部模型的
  宽松构造行为）。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from reposage.domain.enums import FindingCategory, Severity
from reposage.domain.finding import FindingCandidate

# 枚举白名单（小写，与 enums 值一致；模型输出统一归一后再校验）
_SEVERITY_VALUES = {s.value for s in Severity}
_CATEGORY_VALUES = {c.value for c in FindingCategory}


class StrictFindingCandidateOutput(BaseModel):
    """模型输出 finding 的严格 Schema（模型可能填写的字段，claim 语义）。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    title: str
    severity: str
    confidence: float
    category: str
    claimed_path: str | None = None
    claimed_start_line: int | None = None
    claimed_end_line: int | None = None
    chunk_id: str | None = None
    evidence_ref: str | None = None
    trigger_condition: str = ""
    impact: str = ""
    explanation: str = ""
    suggestion: str = ""
    is_outside_diff: bool = False

    @field_validator("severity")
    @classmethod
    def _severity_whitelist(cls, v: str) -> str:
        if v not in _SEVERITY_VALUES:
            raise ValueError(f"severity 不在白名单: {v!r}")
        return v

    @field_validator("category")
    @classmethod
    def _category_whitelist(cls, v: str) -> str:
        if v not in _CATEGORY_VALUES:
            raise ValueError(f"category 不在白名单: {v!r}")
        return v

    def to_candidate(self) -> FindingCandidate:
        """显式转换为领域 FindingCandidate（evidence 由程序后续生成，默认空）。"""
        return FindingCandidate(**self.model_dump())


class StrictFindingsEnvelope(BaseModel):
    """模型输出信封（严格）：findings 必填；summary 可选（V1 摘要落 V1-e）。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    findings: list[StrictFindingCandidateOutput]
    summary: dict[str, Any] = Field(default_factory=dict)


def envelope_json_schema() -> dict[str, Any]:
    """OpenAI json_schema 响应格式用的 JSON Schema（schema_first 策略）。"""
    return StrictFindingsEnvelope.model_json_schema()


def normalize_enums(record: dict[str, Any]) -> dict[str, Any]:
    """枚举白名单映射：把 severity/category 归一为小写字符串。

    模型可能输出 "HIGH"/"High"/" high "，统一转小写后交给严格校验；
    不在白名单的值保持原样，由校验阶段拒绝（触发修复重试）。
    """
    out = dict(record)
    for key, allowed in (("severity", _SEVERITY_VALUES), ("category", _CATEGORY_VALUES)):
        value = out.get(key)
        if isinstance(value, str):
            lowered = value.strip().lower()
            out[key] = lowered if lowered in allowed else value  # 非法值原样保留以便报错
    return out


def parse_envelope(text: str) -> StrictFindingsEnvelope:
    """解析模型输出文本为严格信封。

    - ``findings`` 缺失或为 null/非数组 → ValueError（触发修复重试，P1-2）；
    - 非 JSON → json.JSONDecodeError；
    - 类型/白名单/未知字段违规 → pydantic.ValidationError；
    调用方对以上错误执行单次修复重试（09 §3），仍失败则 fail-soft。
    """
    payload = json.loads(text)  # JSONDecodeError 向上抛
    if not isinstance(payload, dict):
        raise ValueError(f"信封必须是对象，得到 {type(payload).__name__}")
    if "findings" not in payload:
        raise ValueError("信封缺少 findings 字段")
    findings = payload["findings"]
    if not isinstance(findings, list):
        raise ValueError(f"findings 必须是数组，得到 {type(findings).__name__}")
    normalized = [normalize_enums(item) for item in findings]
    return StrictFindingsEnvelope.model_validate({**payload, "findings": normalized})


def envelope_to_candidates(envelope: StrictFindingsEnvelope) -> list[FindingCandidate]:
    """严格信封 → 领域候选列表（显式转换，P1-1）。"""
    return [f.to_candidate() for f in envelope.findings]


__all__ = [
    "StrictFindingCandidateOutput",
    "StrictFindingsEnvelope",
    "envelope_json_schema",
    "envelope_to_candidates",
    "normalize_enums",
    "parse_envelope",
]
