"""内置规则集与文本级匹配器（review/builtin_rules.py）。

V1-b：L4 内置规则（06 §1/§3），少量高价值确定性规则；匹配基于**新增行**
（只审本次变更，避免对历史代码误报）。规则命中产出 L4 上下文 chunk
（ContextSourceKind.RULES），同时可转 CoverageItem（stage=review）。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..domain.enums import DiffLineType, FindingCategory, Severity
from ..domain.models import ChangedFile


class BuiltinRule(BaseModel):
    """内置规则定义（程序打包，最高优先级，06 §3）。"""

    rule_id: str
    severity: Severity
    category: FindingCategory
    description: str
    pattern: str = Field(description="正则（search 语义，作用于单行新增内容）")
    languages: list[str] = Field(default_factory=lambda: ["python"])

    def match_line(self, line: str) -> bool:
        return re.search(self.pattern, line) is not None


class RuleHit(BaseModel):
    """规则命中（确定性证据，非模型输出）。"""

    rule_id: str
    severity: Severity
    category: FindingCategory
    path: str
    line: int | None = None
    message: str


BUILTIN_RULES: list[BuiltinRule] = [
    BuiltinRule(
        rule_id="python.01",
        severity=Severity.HIGH,
        category=FindingCategory.SECURITY,
        description="动态代码执行：eval/exec 处理不可信输入可能导致任意代码执行",
        pattern=r"\b(eval|exec)\s*\(",
    ),
    BuiltinRule(
        rule_id="python.02",
        severity=Severity.HIGH,
        category=FindingCategory.SECURITY,
        description="命令注入：os.system / subprocess(shell=True) 拼接不可信输入有注入风险",
        pattern=r"\b(os\.system|subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True)\b",
    ),
    BuiltinRule(
        rule_id="python.03",
        severity=Severity.HIGH,
        category=FindingCategory.SECURITY,
        description="不安全的反序列化：pickle.loads 处理不可信输入可导致任意代码执行",
        pattern=r"\b(pickle|cloudpickle|joblib)\s*\.\s*loads?\s*\(",
    ),
    BuiltinRule(
        rule_id="python.04",
        severity=Severity.MEDIUM,
        category=FindingCategory.CORRECTNESS,
        description="生产代码使用 assert：-O 下被移除，不能作为输入校验",
        pattern=r"^\s*assert\s+",
    ),
    BuiltinRule(
        rule_id="python.05",
        severity=Severity.HIGH,
        category=FindingCategory.SECURITY,
        description="疑似硬编码密钥/口令：不要将密钥提交到仓库",
        pattern=r"(api[_-]?key|secret|password|passwd|token)\s*=\s*[\"'][A-Za-z0-9_\-\.]{8,}[\"']",
    ),
    BuiltinRule(
        rule_id="python.06",
        severity=Severity.HIGH,
        category=FindingCategory.SECURITY,
        description="SQL 字符串拼接：f-string/format 拼接 SQL 有注入风险，应使用参数化查询",
        pattern=r"\.execute\s*\(\s*f[\"']",
    ),
]


def _rule_map(rules: list[BuiltinRule] | None = None) -> list[BuiltinRule]:
    return rules if rules is not None else BUILTIN_RULES


def _language_of(path: str) -> str | None:
    lower = path.lower()
    if lower.endswith((".py", ".pyi")):
        return "python"
    return None


def _added_lines(file: ChangedFile) -> list[tuple[int, str]]:
    """提取新增行（new_ln, content），供规则匹配（只审本次变更）。"""
    out: list[tuple[int, str]] = []
    for hunk in file.hunks:
        for line in hunk.lines:
            if line.type is DiffLineType.ADDED and line.new_ln is not None:
                out.append((line.new_ln, line.content))
    return out


def match_rules(
    file: ChangedFile,
    rules: list[BuiltinRule] | None = None,
) -> list[RuleHit]:
    """对新增行逐行匹配内置规则（V1-b；规则引擎/静态分析集成见 V1-d）。"""
    lang = _language_of(file.path)
    if lang is None:
        return []
    hits: list[RuleHit] = []
    for rule in _rule_map(rules):
        if lang not in rule.languages:
            continue
        for ln, content in _added_lines(file):
            if rule.match_line(content):
                hits.append(
                    RuleHit(
                        rule_id=rule.rule_id,
                        severity=rule.severity,
                        category=rule.category,
                        path=file.path,
                        line=ln,
                        message=rule.description,
                    )
                )
                break  # 每条规则每文件最多一条命中（锚定首个位置）
    return hits
