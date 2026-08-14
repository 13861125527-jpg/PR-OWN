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
    CoverageItem,
    CoverageManifest,
    DiffHunk,
    DiffLine,
    ReviewContext,
    ReviewUnit,
)
from .builtin_rules import BuiltinRule, RuleHit, match_rules

# ---- Token 估算（06 §2：字符/4 近似，需实测校准） ----


def estimate_tokens(text: str) -> int:
    """字符数/4 的近似 token 估算。"""
    return max(1, math.ceil(len(text) / 4))


# ---- 预算模型（06 §2 分配） ----


class ContextBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_tokens: int = Field(default=32000, gt=0, description="模型总窗口")
    # 输出预留：至少 15%（架构 06 §2），输入预算 = total - output_reserve
    output_reserve_ratio: float = Field(default=0.15, ge=0.15, le=0.5)
    # 输入内部分配比例（总和=1；V1 无 L3，其 25% 释放为输入弹性 reserve，文档化设计决定）
    l0_ratio: float = Field(default=0.05, ge=0.0, le=1.0)
    l1_ratio: float = Field(default=0.05, ge=0.0, le=1.0)
    l2_ratio: float = Field(default=0.40, ge=0.0, le=1.0)
    l4_ratio: float = Field(default=0.10, ge=0.0, le=1.0)
    reserve_ratio: float = Field(default=0.40, ge=0.0, le=1.0)  # 输入弹性（L3 25% 释放 + 15% 余量）

    @model_validator(mode="after")
    def _ratio_sum(self) -> ContextBudget:
        total = self.l0_ratio + self.l1_ratio + self.l2_ratio + self.l4_ratio + self.reserve_ratio
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"上下文比例之和必须为 1：{total:.3f}")
        return self

    @property
    def output_reserve_tokens(self) -> int:
        """模型输出保留空间（输入不得占用）。"""
        return int(self.total_tokens * self.output_reserve_ratio)

    @property
    def input_limit(self) -> int:
        """输入预算上限 = 总窗口 - 输出预留。L2 分块/分组基于此。"""
        return self.total_tokens - self.output_reserve_tokens

    @property
    def l0_tokens(self) -> int:
        return int(self.input_limit * self.l0_ratio)

    @property
    def l1_tokens(self) -> int:
        return int(self.input_limit * self.l1_ratio)

    @property
    def l2_tokens(self) -> int:
        return int(self.input_limit * self.l2_ratio)

    @property
    def l4_tokens(self) -> int:
        return int(self.input_limit * self.l4_ratio)


class ContextBudgetError(ValueError):
    """预算无法满足装配要求（L0 超输入预算 / L1 必保留字段超输入预算）。"""


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


_TRUNCATED_MARKER_MAX = 80  # "[TRUNCATED: N lines omitted from PR metadata]" 上界字符数


def _l1_chunk(req: ChangeRequest, trim_target_tokens: int, hard_cap_tokens: int) -> tuple[ContextChunk, int]:
    """L1 PR 元数据（不可信内容，整体包装；token-aware 逐行裁剪）。

    预算模型（P1-2 返工）：
    - 必保留字段（程序事实）：source / base SHA / head SHA——总大小不得超过
      hard_cap（输入预算剩余），否则抛 ContextBudgetError；
    - 可选字段（半可信）：title → author → is_draft → description——按优先级
      逐行放入 trim_target 预算，超出则裁剪；
    - 借用规则（文档化）：可选字段预算 = trim_target - 必保留；若为负（必保留
      已超名义层预算），从输入弹性 reserve 借用，可选字段全部裁剪；
    - 预算计算基于最终包装后的完整字符串（wrap 标记、前缀、换行、TRUNCATED
      标记全部计入），保证 chunk.tokens <= max(trim_target, 必保留实际)。
    """
    mandatory = [
        f"source: {req.source.value}",
        f"base: {req.base.sha}",
        f"head: {req.head.sha}",
    ]
    optional: list[tuple[str, str | None]] = [
        ("title", req.title),
        ("author", req.author),
        ("is_draft", None if req.is_draft is None else str(req.is_draft)),
        ("description", req.description),
    ]

    wrap_chars = len(wrap_untrusted(""))
    budget_chars = trim_target_tokens * 4 - wrap_chars - _TRUNCATED_MARKER_MAX
    hard_cap_chars = hard_cap_tokens * 4 - wrap_chars - _TRUNCATED_MARKER_MAX

    mandatory_text = "\n".join(mandatory)
    mandatory_chars = len(mandatory_text) + (len(mandatory) - 1)  # 行间换行
    if mandatory_chars > hard_cap_chars:
        raise ContextBudgetError(
            f"L1 必保留字段（source/base/head）超过输入预算剩余"
            f"（{mandatory_chars} 字符 > {hard_cap_chars}），配置需调整"
        )

    lines = list(mandatory)
    used = mandatory_chars
    omitted = 0
    for key, val in optional:
        if val is None:
            continue
        if key == "description":
            for line in val.splitlines():
                entry = f"description: {line}"
                if used + len(entry) + 1 > budget_chars:
                    omitted += 1  # 真实丢弃（含借用时 budget_chars < mandatory 的情况）
                    continue
                lines.append(entry)
                used += len(entry) + 1
            continue
        entry = f"{key}: {val}"
        if used + len(entry) + 1 > budget_chars:
            omitted += 1
            continue
        lines.append(entry)
        used += len(entry) + 1

    if omitted:
        lines.append(f"[TRUNCATED: {omitted} lines omitted from PR metadata]")
    content = "\n".join(lines)
    chunk = ContextChunk(
        layer=ContextLayer.L1,
        source=ContextSource(kind=ContextSourceKind.ISSUE, ref="pr:metadata"),
        content=wrap_untrusted(content),
        tokens=estimate_tokens(wrap_untrusted(content)),
        truncated=omitted > 0,
    )
    return chunk, omitted


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


def _chunk_l4(
    file: ChangedFile,
    rules: list[BuiltinRule],
    max_tokens: int,
) -> tuple[list[ContextChunk], list[RuleHit], list[RuleHit]]:
    """L4 命中规则 → chunk；预算不足时裁掉低 severity 规则。

    返回 (chunks, kept_hits, dropped_hits)：kept 真正进入上下文；dropped 因预算
    未进入（P2-1：Coverage 必须区分二者，不能把 dropped 标为 COVERED）。
    """
    hits = sorted(match_rules(file, rules), key=lambda h: _severity_rank(h.severity), reverse=True)
    chunks: list[ContextChunk] = []
    kept: list[RuleHit] = []
    dropped: list[RuleHit] = []
    used = 0
    for hit in hits:
        text = f"[{hit.rule_id}] {hit.message} (path={hit.path}, line={hit.line})"
        tok = estimate_tokens(text)
        if used + tok > max_tokens:
            dropped.append(hit)  # 真实丢失（计入 truncated 与 Coverage）
            continue
        chunks.append(
            ContextChunk(
                layer=ContextLayer.L4,
                source=ContextSource(kind=ContextSourceKind.RULES, ref=f"rule:{hit.rule_id}"),
                content=text,
                tokens=tok,
            )
        )
        kept.append(hit)
        used += tok
    return chunks, kept, dropped


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


# ---- 覆盖清单（直接从装配结果生成：kept/dropped 规则与 L1 裁剪，不重新匹配） ----


def _coverage_for(
    file: ChangedFile,
    *,
    kept_rules: list[RuleHit],
    dropped_rules: list[RuleHit],
    l1_truncated: bool,
    l1_omitted: int,
    stage: StageName,
) -> CoverageManifest:
    """由实际装载结果生成覆盖记录（P2-1/P2-2）。

    - 文件本身：COVERED；
    - 实际进入 L4 的规则：COVERED；
    - 命中但因预算未进入 L4 的规则：TRUNCATED（独立 item，detail 带 rule_id）；
    - L1 真实裁剪：TRUNCATED（独立 item，detail 带省略行数）。
    每种丢失都是独立 CoverageItem，不压缩成单个字符串。
    """
    items: list[CoverageItem] = [
        CoverageItem(target=file.path, reason=CoverageReason.COVERED, stage=stage)
    ]
    for hit in kept_rules:
        items.append(
            CoverageItem(
                target=hit.rule_id,
                reason=CoverageReason.COVERED,
                stage=stage,
                detail=f"{hit.path}:{hit.line}",
            )
        )
    for hit in dropped_rules:
        items.append(
            CoverageItem(
                target=hit.rule_id,
                reason=CoverageReason.TRUNCATED,
                stage=stage,
                detail=f"L4 budget dropped: {hit.path}:{hit.line}",
            )
        )
    if l1_truncated:
        items.append(
            CoverageItem(
                target=file.path,
                reason=CoverageReason.TRUNCATED,
                stage=stage,
                detail=f"L1 metadata truncated: {l1_omitted} lines",
            )
        )
    truncated = bool(dropped_rules) or l1_truncated
    return CoverageManifest(items=items, truncated=truncated)


# ---- 装配器（per-file map-reduce，产出多个独立 ReviewUnit；ReviewUnit 定义在 domain/models） ----


class ContextAssembler:
    """为单个 changed file 装配 1..N 个自包含、不超预算的 ReviewUnit（models 定义）。"""

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
        input_limit = budget.input_limit
        output_reserve = budget.output_reserve_tokens

        # L0：治理文本（不可裁）。若 L0 本身就超出输入预算（输出空间优先），明确失败
        l0 = _l0_chunk(run_id)
        if l0.tokens > input_limit:
            raise ContextBudgetError(
                f"L0 治理文本本身超过输入预算（{l0.tokens} > {input_limit}），配置需调整"
            )

        # L1：必保留字段不得超输入剩余；可选字段按名义层预算裁剪（借用规则见 _l1_chunk）
        l1_available = input_limit - l0.tokens
        l1, l1_omitted = _l1_chunk(
            change_request,
            trim_target_tokens=min(budget.l1_tokens, l1_available),
            hard_cap_tokens=l1_available,
        )

        # L4：预算 = min(名义 l4, 输入剩余)；kept/dropped 供 Coverage
        l4_available = input_limit - l0.tokens - l1.tokens
        l4_chunks, l4_kept, l4_dropped = _chunk_l4(
            file, self.rules, max_tokens=min(budget.l4_tokens, l4_available)
        )

        # L2：输入预算剩余（= input_limit - 已装配固定层），不占用输出预留
        l2_budget = input_limit - l0.tokens - l1.tokens - sum(c.tokens for c in l4_chunks)
        l2_chunks = _chunk_l2(file, chunk_max_tokens=l2_budget) if l2_budget > 0 else []
        groups = _group_l2_chunks(l2_chunks, l2_budget) if l2_chunks else [[]]

        units: list[ReviewUnit] = []
        for index, group in enumerate(groups, start=1):
            chunks = [l0, l1, *group, *l4_chunks]
            total = sum(c.tokens for c in chunks)
            truncated = l1.truncated or bool(l4_dropped)
            context = ReviewContext(
                run_id=run_id,
                chunks=chunks,
                total_tokens=total,
                budget_tokens=input_limit,  # 硬断言校验输入预算（不含输出预留）
                truncated=truncated,
            )
            context.assert_within_budget()  # P1-1 硬断言 1：unit 输入 <= input_limit
            if output_reserve < budget.output_reserve_tokens:  # pragma: no cover
                raise ContextBudgetError(
                    f"输出预留不足（{output_reserve} < {budget.output_reserve_tokens}）"
                )
            unit_id = file.path if len(groups) == 1 else f"{file.path}#{index}"
            units.append(
                ReviewUnit(
                    unit_id=unit_id,
                    file_path=file.path,
                    context=context,
                    truncated=truncated,
                    coverage=_coverage_for(
                        file,
                        kept_rules=l4_kept,
                        dropped_rules=l4_dropped,
                        l1_truncated=l1.truncated,
                        l1_omitted=l1_omitted,
                        stage=stage,
                    ),
                    input_limit=input_limit,
                    output_reserve_tokens=output_reserve,
                    total_window_tokens=budget.total_tokens,
                )
            )
        return units


# ---- 消息组装（V1-d：ReviewUnit → LLM 消息） ----


def unit_to_messages(unit: ReviewUnit) -> list[dict[str, str]]:
    """把 unit 的 L0/L4（可信：治理+规则）与 L1/L2（不可信内容，已 UNTRUSTED 包装）组装为 system/user 消息。

    L0/L4 由程序生成（可信）；L1/L2 为不可信内容（已带 [UNTRUSTED_CONTENT] 边界），
    统一放入 user 侧，避免与治理指令混合。
    """
    system = [c.content for c in unit.context.chunks if c.layer in (ContextLayer.L0, ContextLayer.L4)]
    user = [c.content for c in unit.context.chunks if c.layer in (ContextLayer.L1, ContextLayer.L2)]
    return [
        {"role": "system", "content": "\n\n".join(system)},
        {"role": "user", "content": "\n\n".join(user)},
    ]
