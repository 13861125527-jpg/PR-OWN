"""上下文构建器（review/context.py）。

V1-b（12 §2）：确定性装配 L0–L2（+ L4 内置规则）+ 预算规划，per-file map-reduce 契约
（04 §1：按 changed file 建任务；每文件可产出多个 ReviewUnit，每个 unit 是一次
模型调用可容纳的自包含上下文）。

设计要点（验收返工）：
- **分块 ≠ 截断**：L2 大 hunk 拆成多个自包含子块（每块带 hunk header 与行号范围），
  不设置 truncated；只有内容真正丢失才写 [TRUNCATED: N lines omitted from ...]。
- **总预算硬约束**：每个 ReviewUnit.total_tokens <= budget_tokens（assert_within_budget），
  大文件超单次预算 → 产出多个独立 unit，而不是返回一个超预算上下文。
- **安全边界**：L1/L2 属于不可信内容（PR 描述、代码），统一用
  [UNTRUSTED_CONTENT]...[/UNTRUSTED_CONTENT] 包装（09 §7 / 10 §3），防止提示注入
  与治理提示混合。
- **裁剪顺序**（06 §2）：L4 先裁（按 severity 保留）→ L1 token-aware 裁（记录截断
  状态与省略数）→ L2 永不裁剪（多 unit 承担）→ L0 不可裁（唯一允许的超预算失败点）。

预算比例（06 §2 / ContextConfig）：L0 5% / L1 5% / L2 40% / L4 10% / 余量 40%
（V1 无 L3：25% L3 预留 + 15% 输出空间）。
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
    DiffLine,
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


class ContextBudgetError(ValueError):
    """预算无法满足装配要求（唯一允许：L0 治理文本本身超预算）。"""


# ---- 不可信内容边界（09 §7 / 10 §3） ----


def wrap_untrusted(content: str) -> str:
    """给不可信内容加统一边界标记，防止提示注入与治理指令混合。"""
    return f"[UNTRUSTED_CONTENT]\n{content}\n[/UNTRUSTED_CONTENT]"


_WRAP_OVERHEAD = estimate_tokens(wrap_untrusted(""))  # 边界标记本身的 token 开销


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


def _l1_chunk(req: ChangeRequest, max_tokens: int) -> ContextChunk:
    """L1 PR 元数据（不可信内容，整体包装；description 按行 token-aware 裁剪）。

    固定字段（source/title/sha/author/is_draft）总是保留；description 是主要可变部分，
    超出剩余预算时逐行裁剪，并在裁剪处写 [TRUNCATED: N lines omitted ...] 明文。
    """
    fixed = [
        f"source: {req.source.value}",
        f"title: {req.title or ''}",
        f"base: {req.base.sha}",
        f"head: {req.head.sha}",
        f"author: {req.author or ''}",
        f"is_draft: {req.is_draft}",
    ]
    fixed_text = "\n".join(fixed)
    remaining = max(0, max_tokens - estimate_tokens(fixed_text))

    desc_lines = (req.description or "").splitlines()
    kept: list[str] = []
    used = 0
    omitted = 0
    for line in desc_lines:
        tok = estimate_tokens(line)
        if used + tok > remaining:
            omitted += 1  # 真正丢弃该行（含 remaining=0 的极端情况）
            continue
        kept.append(line)
        used += tok

    parts = [f"description: {line}" for line in kept] if kept else []
    if omitted:
        parts.append(f"[TRUNCATED: {omitted} lines omitted from PR description]")
    content = "\n".join([fixed_text, *parts]) if parts else fixed_text
    return ContextChunk(
        layer=ContextLayer.L1,
        source=ContextSource(kind=ContextSourceKind.ISSUE, ref="pr:metadata"),
        content=wrap_untrusted(content),
        tokens=estimate_tokens(wrap_untrusted(content)),
        truncated=omitted > 0,
    )


# ---- L2 diff 分块（自包含；分块 ≠ 截断） ----


def _format_diff_line(ln: DiffLine) -> str:
    if ln.type.value == "context":
        return f" {ln.content}"
    if ln.type.value == "removed":
        return f"-{ln.content}"
    return f"+{ln.content}"


def _hunk_text(hunk: DiffHunk) -> str:
    return "\n".join([hunk.header, *(_format_diff_line(ln) for ln in hunk.lines)])


def _split_hunk(file_path: str, hunk: DiffHunk, chunk_max_tokens: int) -> list[ContextChunk]:
    """把超大 hunk 拆成自包含子块。

    每个子块都包含 hunk header 与行号范围标注（new 侧起止），保证"块内自包含"：
    @@ -1,100 +1,100 @@  [new lines 1-34]
    """
    new_nums = [ln.new_ln for ln in hunk.lines if ln.new_ln is not None]
    _lo = min(new_nums) if new_nums else 1
    _hi = max(new_nums) if new_nums else 1
    # header 行字符上界（实际 start/end 不会超过区间 → 文本不会更长）
    header_upper_len = len(f"{hunk.header}  [new lines {_lo}-{_hi}]")
    wrap_chars = len(wrap_untrusted(""))  # 边界标记字符数
    budget_chars = chunk_max_tokens * 4  # token 估算 = 字符/4，用字符级精确比较

    sub_chunks: list[ContextChunk] = []
    buffer: list[tuple[str, int | None]] = []  # (formatted line, new_ln)
    buffer_chars = 0  # 已放行字符（含 header 占位与换行）
    first_new: int | None = None

    def flush() -> None:
        nonlocal buffer, buffer_chars, first_new
        if not buffer:
            return
        start = first_new if first_new is not None else _lo
        end = buffer[-1][1] if buffer[-1][1] is not None else (start + len(buffer) - 1)
        header = f"{hunk.header}  [new lines {start}-{end}]"
        content = "\n".join([header, *(line for line, _ in buffer)])
        sub_chunks.append(
            ContextChunk(
                layer=ContextLayer.L2,
                source=ContextSource(kind=ContextSourceKind.DIFF, ref=f"file:{file_path}"),
                content=wrap_untrusted(content),
                tokens=estimate_tokens(wrap_untrusted(content)),
            )
        )
        buffer = []
        buffer_chars = 0
        first_new = None

    for ln in hunk.lines:
        raw = _format_diff_line(ln)
        if header_upper_len + 1 + len(raw) + wrap_chars > budget_chars:
            # 单行 diff 加上头部标注都超过整个 L2 分块上限（如 minified 行）：
            # 无法自包含分块，数学上无法满足预算 → 明确失败（配置病态，而非静默超预算）
            raise ContextBudgetError(
                f"单行 diff 超过 L2 分块上限（{len(raw)} 字符 > {budget_chars}），无法自包含分块"
            )
        if buffer and buffer_chars + 1 + len(raw) + wrap_chars > budget_chars:
            flush()
        if not buffer:
            first_new = ln.new_ln
            buffer_chars = header_upper_len  # 本块 header 行占位
        buffer.append((raw, ln.new_ln))
        buffer_chars += 1 + len(raw)  # 换行符 + 行内容
    flush()
    return sub_chunks


def _chunk_l2(file: ChangedFile, chunk_max_tokens: int) -> list[ContextChunk]:
    """L2 分块：按 hunk 分块；单 hunk 超限拆自包含子块。

    分块不设置 truncated——内容全部保留（无丢失）。总预算约束由上层分组到
    多个 ReviewUnit 承担（当前任务 L2 永不裁剪，06 §2）。
    """
    chunks: list[ContextChunk] = []
    for hunk in file.hunks:
        if estimate_tokens(_hunk_text(hunk)) + _WRAP_OVERHEAD <= chunk_max_tokens:
            content = wrap_untrusted(_hunk_text(hunk))
            chunks.append(
                ContextChunk(
                    layer=ContextLayer.L2,
                    source=ContextSource(kind=ContextSourceKind.DIFF, ref=f"file:{file.path}"),
                    content=content,
                    tokens=estimate_tokens(content),
                )
            )
            continue
        chunks.extend(_split_hunk(file.path, hunk, chunk_max_tokens))
    return chunks


# ---- L4 内置规则命中（程序生成，可信；预算内按 severity 保留） ----


def _severity_rank(sev: Severity) -> int:
    """severity 数值等级（StrEnum 字典序不可靠）。"""
    return {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}.get(sev.value, 0)


def _chunk_l4(file: ChangedFile, rules: list[BuiltinRule], max_tokens: int) -> tuple[list[ContextChunk], int]:
    """L4 命中规则 → chunk；预算不足时裁掉低 severity 规则，返回 (chunks, dropped)。"""
    hits = sorted(match_rules(file, rules), key=lambda h: _severity_rank(h.severity), reverse=True)
    chunks: list[ContextChunk] = []
    used = 0
    dropped = 0
    for hit in hits:
        text = f"[{hit.rule_id}] {hit.message} (path={hit.path}, line={hit.line})"
        tok = estimate_tokens(text)
        if used + tok > max_tokens:
            dropped += 1  # 规则命中被丢弃（真实丢失 → 计入 truncated）
            continue
        chunks.append(
            ContextChunk(
                layer=ContextLayer.L4,
                source=ContextSource(kind=ContextSourceKind.RULES, ref=f"rule:{hit.rule_id}"),
                content=text,
                tokens=tok,
            )
        )
        used += tok
    return chunks, dropped


def _group_l2_chunks(chunks: list[ContextChunk], per_unit_budget: int) -> list[list[ContextChunk]]:
    """把 L2 分块贪心分组，每组 token 和 <= per_unit_budget → 一个 ReviewUnit。"""
    if not chunks:
        return []
    groups: list[list[ContextChunk]] = []
    current: list[ContextChunk] = []
    used = 0
    for c in chunks:
        if current and used + c.tokens > per_unit_budget:
            groups.append(current)
            current = []
            used = 0
        current.append(c)
        used += c.tokens
    if current:
        groups.append(current)
    return groups


# ---- 覆盖清单（从装配结果生成，不手工传布尔） ----


def _coverage_for(
    file: ChangedFile,
    rules: list[BuiltinRule],
    *,
    truncated: bool,
    truncated_detail: str,
    stage: StageName,
) -> CoverageManifest:
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
    if truncated:
        items.append(
            CoverageItem(
                target=file.path,
                reason=CoverageReason.TRUNCATED,
                stage=stage,
                detail=truncated_detail,
            )
        )
    return CoverageManifest(items=items, truncated=truncated)


# ---- 装配器（per-file map-reduce，产出多个独立 ReviewUnit） ----


class ReviewUnit(BaseModel):
    """单次模型调用的上下文单位（04 §1 per-file map-reduce 的原子任务粒度）。

    一个 changed file 可产出多个 unit（大文件超单次预算时分块），每个 unit 自包含
    L0/L1/L2/L4，且 context.total_tokens <= context.budget_tokens。
    """

    unit_id: str
    file_path: str
    context: ReviewContext
    truncated: bool = Field(description="本 unit 内是否真实丢失内容（L1/L4 裁剪）")
    coverage: CoverageManifest


class ContextAssembler:
    """为单个 changed file 装配 1..N 个自包含、不超预算的 ReviewUnit。"""

    def __init__(
        self,
        budget: ContextBudget | None = None,
        rules: list[BuiltinRule] | None = None,
    ) -> None:
        from .builtin_rules import BUILTIN_RULES

        self.budget = budget or ContextBudget()
        self.rules = rules if rules is not None else BUILTIN_RULES

    def build_file_units(
        self,
        *,
        run_id: str,
        change_request: ChangeRequest,
        file: ChangedFile,
        stage: StageName = StageName.CONTEXT,
    ) -> list[ReviewUnit]:
        budget = self.budget
        l0 = _l0_chunk(run_id)
        if l0.tokens > budget.total_tokens:
            raise ContextBudgetError(
                f"L0 治理文本本身超过总预算（{l0.tokens} > {budget.total_tokens}），配置需调整"
            )

        l1 = _l1_chunk(change_request, budget.l1_tokens)
        l4, l4_dropped = _chunk_l4(file, self.rules, budget.l4_tokens)

        fixed_tokens = l0.tokens + l1.tokens + sum(c.tokens for c in l4)
        l2_budget = budget.total_tokens - fixed_tokens
        if l2_budget < 0 and file.hunks:
            raise ContextBudgetError(
                f"L0+L1+L4 固定开销超过总预算（{fixed_tokens} > {budget.total_tokens}），"
                "无法容纳任何 L2 diff，配置需调整"
            )

        l2_chunks = _chunk_l2(file, chunk_max_tokens=l2_budget) if l2_budget > 0 else []
        groups = _group_l2_chunks(l2_chunks, l2_budget) if l2_chunks else [[]]

        units: list[ReviewUnit] = []
        for index, group in enumerate(groups, start=1):
            chunks = [l0, l1, *group, *l4]
            total = sum(c.tokens for c in chunks)
            truncated = l1.truncated or l4_dropped > 0
            detail = (
                "L1 description truncated" if l1.truncated else "L4 rules dropped"
                if l4_dropped > 0
                else "none"
            )
            context = ReviewContext(
                run_id=run_id,
                chunks=chunks,
                total_tokens=total,
                budget_tokens=budget.total_tokens,
                truncated=truncated,
            )
            context.assert_within_budget()  # 硬断言：unit 不得超预算
            unit_id = file.path if len(groups) == 1 else f"{file.path}#{index}"
            units.append(
                ReviewUnit(
                    unit_id=unit_id,
                    file_path=file.path,
                    context=context,
                    truncated=truncated,
                    coverage=_coverage_for(
                        file,
                        self.rules,
                        truncated=truncated,
                        truncated_detail=detail,
                        stage=stage,
                    ),
                )
            )
        return units
