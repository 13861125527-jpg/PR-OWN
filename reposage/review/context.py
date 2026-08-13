"""上下文构建器（review/context.py）。

V1-b（12 §2）：确定性装配 L0–L2（+ L4 内置规则）+ 预算裁剪，per-file map-reduce 契约
（04 §1：按 changed file 建任务，每文件上下文 = 该文件 L0/L1/L2/L4）。

预算与裁剪顺序遵循 06 §2：
- L0 治理 5%（恒定，不可裁）、L1 PR 元数据 5%、L2 diff 40%（当前任务永不裁剪）、
  L4 规则 10%、余量 40%（V1 无 L3：25% L3 预留 + 15% 输出空间）
- 裁剪顺序：L4 先裁（保留高 severity）→ 非当前任务 L2（per-file 上下文无）→ L0 不可裁
- 分块 token-aware（字符/4 估算）；截断处写 [TRUNCATED: N lines omitted] 明文（06 §2/§3）
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain.enums import ContextLayer, ContextSourceKind, CoverageReason, Severity, StageName
from ..domain.models import (
    ChangedFile,
    ChangeRequest,
    ContextChunk,
    ContextSource,
    DiffHunk,
    ReviewContext,
)
from ..domain.run import CoverageItem, CoverageManifest
from .builtin_rules import BuiltinRule, match_rules

# ---- Token 估算（06 §2：字符/4 近似，需实测校准） ----


def estimate_tokens(text: str) -> int:
    """字符数/4 的近似 token 估算。"""
    return max(1, math.ceil(len(text) / 4))


# ---- 预算模型（06 §2 分配） ----


class ContextBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_tokens: int = Field(default=32000, gt=0)
    l0_ratio: float = Field(default=0.05, ge=0.0, le=1.0)
    l1_ratio: float = Field(default=0.05, ge=0.0, le=1.0)
    l2_ratio: float = Field(default=0.40, ge=0.0, le=1.0)
    l4_ratio: float = Field(default=0.10, ge=0.0, le=1.0)
    reserve_ratio: float = Field(default=0.40, ge=0.0, le=1.0)  # V1 无 L3：25% L3 预留 + 15% 余量

    @model_validator(mode="after")
    def _ratio_sum(self) -> ContextBudget:
        total = self.l0_ratio + self.l1_ratio + self.l2_ratio + self.l4_ratio + self.reserve_ratio
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"上下文比例之和必须为 1：{total:.3f}")
        return self

    @property
    def l0_tokens(self) -> int:
        return int(self.total_tokens * self.l0_ratio)

    @property
    def l1_tokens(self) -> int:
        return int(self.total_tokens * self.l1_ratio)

    @property
    def l2_tokens(self) -> int:
        return int(self.total_tokens * self.l2_ratio)

    @property
    def l4_tokens(self) -> int:
        return int(self.total_tokens * self.l4_ratio)


# ---- L0 治理文本（固定，不可裁） ----

_L0_GOVERNANCE = """\
你是 RepoSage 代码审查代理。
输出协议：
- 只提交模型主张的 claimed 位置（路径与行号），canonical 字段由程序确认；
- 每条 finding 必须引用 diff 新增行或证据来源；
- 不修改仓库文件、不执行代码、不访问网络（除非工具显式提供）。
禁止：编造证据、声称已验证未验证的内容、对 diff 之外的历史代码发表行内评论。"""


def _l0_chunk(run_id: str) -> ContextChunk:
    return ContextChunk(
        layer=ContextLayer.L0,
        source=ContextSource(kind=ContextSourceKind.SYSTEM, ref="system:governance"),
        content=_L0_GOVERNANCE,
        tokens=estimate_tokens(_L0_GOVERNANCE),
    )


def _l1_chunk(req: ChangeRequest) -> ContextChunk:
    meta = [
        f"source: {req.source.value}",
        f"title: {req.title or ''}",
        f"base: {req.base.sha}",
        f"head: {req.head.sha}",
        f"author: {req.author or ''}",
        f"is_draft: {req.is_draft}",
    ]
    if req.description:
        meta.append(f"description: {req.description[:2000]}")
    content = "\n".join(meta)
    return ContextChunk(
        layer=ContextLayer.L1,
        source=ContextSource(kind=ContextSourceKind.ISSUE, ref="pr:metadata"),
        content=content,
        tokens=estimate_tokens(content),
    )


def _chunk_hunk_text(hunk: DiffHunk) -> str:
    lines = [hunk.header]
    for ln in hunk.lines:
        if ln.type.value == "context":
            lines.append(f" {ln.content}")
        elif ln.type.value == "removed":
            lines.append(f"-{ln.content}")
        else:
            lines.append(f"+{ln.content}")
    return "\n".join(lines)


def _chunk_l2(file: ChangedFile, max_tokens: int, hunk_max_tokens: int) -> list[ContextChunk]:
    """L2 分块：按 hunk 分块（块内自包含）；单个 hunk 超限按行切分并写 TRUNCATED 标记。

    06 §2：当前任务 L2 永不裁剪——超限采用分块截断而非丢弃，缺省处明文标记。
    """
    chunks: list[ContextChunk] = []
    for hunk in file.hunks:
        text = _chunk_hunk_text(hunk)
        if estimate_tokens(text) <= hunk_max_tokens:
            chunks.append(
                ContextChunk(
                    layer=ContextLayer.L2,
                    source=ContextSource(kind=ContextSourceKind.DIFF, ref=f"file:{file.path}"),
                    content=text,
                    tokens=estimate_tokens(text),
                )
            )
            continue
        # 超限 hunk：按行累积切块；未包含的行以 TRUNCATED 标记
        lines = [hunk.header] + [
            (f" {ln.content}" if ln.type.value == "context" else f"-{ln.content}" if ln.type.value == "removed" else f"+{ln.content}")
            for ln in hunk.lines
        ]
        buffer: list[str] = []
        buf_tokens = 0
        omitted = 0
        for raw in lines:
            tok = estimate_tokens(raw)
            if buffer and buf_tokens + tok > hunk_max_tokens:
                chunks.append(
                    ContextChunk(
                        layer=ContextLayer.L2,
                        source=ContextSource(kind=ContextSourceKind.DIFF, ref=f"file:{file.path}"),
                        content="\n".join(buffer),
                        tokens=estimate_tokens("\n".join(buffer)),
                    )
                )
                buffer = []
                buf_tokens = 0
            buffer.append(raw)
            buf_tokens += tok
        omitted = max(0, len(lines) - len(buffer))
        marker = f"\n[TRUNCATED: {omitted} lines omitted from {file.path}]"
        chunks.append(
            ContextChunk(
                layer=ContextLayer.L2,
                source=ContextSource(kind=ContextSourceKind.DIFF, ref=f"file:{file.path}"),
                content="\n".join(buffer) + marker,
                tokens=estimate_tokens("\n".join(buffer) + marker),
                truncated=True,
            )
        )
    return chunks


def _severity_rank(sev: Severity) -> int:
    """severity 数值等级（StrEnum 字典序不可靠）。"""
    return {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}.get(sev.value, 0)


def _chunk_l4(file: ChangedFile, rules: list[BuiltinRule], max_tokens: int) -> list[ContextChunk]:
    """L4：命中当前文件的规则 → 每规则一个 chunk；预算内按 severity 保留（06 §2 裁剪顺序 1）。"""
    hits = sorted(match_rules(file, rules), key=lambda h: _severity_rank(h.severity), reverse=True)
    chunks: list[ContextChunk] = []
    used = 0
    for hit in hits:
        text = f"[{hit.rule_id}] {hit.message} (path={hit.path}, line={hit.line})"
        tok = estimate_tokens(text)
        if used + tok > max_tokens:
            break  # 预算不足时裁掉更低优先级规则（低 severity 已排后）
        chunks.append(
            ContextChunk(
                layer=ContextLayer.L4,
                source=ContextSource(kind=ContextSourceKind.RULES, ref=f"rule:{hit.rule_id}"),
                content=text,
                tokens=tok,
            )
        )
        used += tok
    return chunks


class ContextAssembler:
    """per-file map-reduce 契约（04 §1）：为单个 changed file 装配 L0–L2 + L4 上下文。"""

    def __init__(
        self,
        budget: ContextBudget | None = None,
        rules: list[BuiltinRule] | None = None,
    ) -> None:
        from .builtin_rules import BUILTIN_RULES

        self.budget = budget or ContextBudget()
        self.rules = rules if rules is not None else BUILTIN_RULES

    def build_file_context(
        self,
        *,
        run_id: str,
        change_request: ChangeRequest,
        file: ChangedFile,
    ) -> ReviewContext:
        chunks: list[ContextChunk] = []
        l0 = _l0_chunk(run_id)
        l1 = _l1_chunk(change_request)
        l2 = _chunk_l2(file, self.budget.l2_tokens, self.budget.l2_tokens)
        l4 = _chunk_l4(file, self.rules, self.budget.l4_tokens)
        chunks = [l0, l1, *l2, *l4]

        total = sum(c.tokens for c in chunks)
        truncated = any(c.truncated for c in chunks) or total > self.budget.total_tokens
        return ReviewContext(
            run_id=run_id,
            chunks=chunks,
            total_tokens=total,
            budget_tokens=self.budget.total_tokens,
            truncated=truncated,
        )


def build_coverage(
    file: ChangedFile,
    rules: list[BuiltinRule],
    *,
    truncated_l2: bool = False,
    stage: StageName = StageName.REVIEW,
) -> CoverageManifest:
    """上下文装配的覆盖清单：规则命中/未命中 + L2 截断（07 §8 / 06 §2 裁剪 3）。"""
    items: list[CoverageItem] = [
        CoverageItem(target=file.path, reason=CoverageReason.COVERED, stage=stage)
    ]
    for hit in match_rules(file, rules):
        items.append(
            CoverageItem(
                target=hit.rule_id,
                reason=CoverageReason.COVERED,
                stage=stage,
                detail=f"{hit.path}:{hit.line}",
            )
        )
    if truncated_l2:
        items.append(
            CoverageItem(
                target=file.path,
                reason=CoverageReason.TRUNCATED,
                stage=stage,
                detail="L2 chunk truncated",
            )
        )
    return CoverageManifest(items=items, truncated=truncated_l2)
