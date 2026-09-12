"""反馈记忆：写入校验、条件化匹配、Pipeline 压制、L4 候选（V2-E）。

契约：`docs/architecture/22-v2e-alignment.md`。纯函数，无 IO。
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable

from reposage.domain.enums import FeedbackKind, FindingCategory, FindingStatus
from reposage.domain.finding import Finding
from reposage.domain.models import is_safe_repo_path
from reposage.domain.run import FeedbackMemory

PATTERN_MAX_LEN = 256
RATIONALE_STORE_MAX = 2000
RATIONALE_L4_MAX = 400
PATTERN_HAYSTACK_MAX = 4096

_CONDITION_FIELDS = (
    "scope",
    "path",
    "symbol",
    "category",
    "rule_key",
    "pattern",
    "cross_run_match_key",
)
_SUPPRESS_KINDS = frozenset({FeedbackKind.FALSE_POSITIVE, FeedbackKind.WONT_FIX})
_GLOB_CHARS = frozenset("*?[]")


class FeedbackConfigError(ValueError):
    """写入校验失败（仅 repo / 非法路径 / 非法正则等）。"""


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def normalize_repo_relpath(raw: str) -> str:
    """posix 相对路径：`\\`→`/`，去掉前导 `./` 与尾部 `/`。"""
    text = raw.replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


def _no_empty_segments(path: str) -> bool:
    return all(part != "" for part in path.split("/"))


def _validate_path_value(raw: str, *, allow_glob: bool) -> str:
    normalized = normalize_repo_relpath(raw)
    if not normalized or not _no_empty_segments(normalized):
        raise FeedbackConfigError("path/scope 不能为空或含空 segment")
    if not allow_glob and any(ch in normalized for ch in _GLOB_CHARS):
        raise FeedbackConfigError("path 不能含 glob 通配符")
    masked = "".join("x" if ch in _GLOB_CHARS else ch for ch in normalized)
    if not is_safe_repo_path(masked):
        raise FeedbackConfigError("path/scope 禁止绝对路径或目录穿越")
    return normalized


def sanitize_rationale(text: str, *, max_len: int) -> str:
    """去掉 C0 控制字符（保留 \\n/\\t），再截断。"""
    cleaned: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch in "\n\t" or code >= 32:
            cleaned.append(ch)
    return "".join(cleaned)[:max_len]


def has_match_condition(memory: FeedbackMemory) -> bool:
    return any(_blank_to_none(getattr(memory, name)) for name in _CONDITION_FIELDS)


def validate_feedback_memory(memory: FeedbackMemory) -> FeedbackMemory:
    """规范化并校验写入条件；失败抛 FeedbackConfigError。"""
    memory.repo = memory.repo.strip()
    if not memory.repo:
        raise FeedbackConfigError("repo 不能为空")
    memory.scope = _blank_to_none(memory.scope)
    memory.path = _blank_to_none(memory.path)
    memory.symbol = _blank_to_none(memory.symbol)
    memory.category = _blank_to_none(memory.category)
    memory.rule_key = _blank_to_none(memory.rule_key)
    memory.pattern = _blank_to_none(memory.pattern)
    memory.cross_run_match_key = _blank_to_none(memory.cross_run_match_key)
    if not has_match_condition(memory):
        raise FeedbackConfigError("禁止仅 repo 的全局压制：至少一项匹配条件")
    if memory.path is not None:
        memory.path = _validate_path_value(memory.path, allow_glob=False)
    if memory.scope is not None:
        memory.scope = _validate_path_value(memory.scope, allow_glob=True)
    if memory.category is not None:
        try:
            FindingCategory(memory.category)
        except ValueError as exc:
            raise FeedbackConfigError(f"未知 category: {memory.category}") from exc
    if memory.pattern is not None:
        if len(memory.pattern) > PATTERN_MAX_LEN:
            raise FeedbackConfigError(f"pattern 长度不能超过 {PATTERN_MAX_LEN}")
        try:
            re.compile(memory.pattern)
        except re.error as exc:
            raise FeedbackConfigError(f"非法 pattern: {exc}") from exc
    memory.rationale = sanitize_rationale(memory.rationale, max_len=RATIONALE_STORE_MAX)
    return memory


def matches_finding(memory: FeedbackMemory, finding: Finding, *, repo: str) -> bool:
    """AND 匹配；未给出的条件字段为通配。仅 active 记忆参与。"""
    if not memory.active or memory.repo != repo:
        return False
    if memory.path is not None:
        if not finding.canonical_path:
            return False
        if normalize_repo_relpath(finding.canonical_path) != memory.path:
            return False
    if memory.scope is not None:
        if not finding.canonical_path:
            return False
        if not fnmatch.fnmatchcase(normalize_repo_relpath(finding.canonical_path), memory.scope):
            return False
    if memory.category is not None and finding.category.value != memory.category:
        return False
    if memory.rule_key is not None and (finding.rule_id is None or finding.rule_id != memory.rule_key):
        return False
    if memory.symbol is not None:
        hay = f"{finding.trigger_condition}\n{finding.title}"
        if memory.symbol not in hay:
            return False
    if memory.pattern is not None:
        haystack = (
            f"{finding.title}\n{finding.trigger_condition}\n{finding.explanation}"
        )[:PATTERN_HAYSTACK_MAX]
        try:
            if re.search(memory.pattern, haystack) is None:
                return False
        except re.error:
            return False
    return not (
        memory.cross_run_match_key is not None
        and finding.cross_run_match_key != memory.cross_run_match_key
    )


def _suppress_id(memory: FeedbackMemory) -> int:
    return memory.id if memory.id is not None else 10**18


def apply_feedback_suppressions(
    findings: list[Finding],
    memories: Iterable[FeedbackMemory],
    *,
    repo: str,
) -> int:
    """对 MERGED / BODY_ONLY 应用压制类记忆。返回新压制条数。"""
    active = [m for m in memories if m.active]
    suppressed = 0
    for finding in findings:
        if finding.status not in {FindingStatus.MERGED, FindingStatus.BODY_ONLY}:
            continue
        hits = [
            m
            for m in active
            if m.kind in _SUPPRESS_KINDS and matches_finding(m, finding, repo=repo)
        ]
        if not hits:
            continue
        chosen = min(hits, key=_suppress_id)
        finding.record_transition(
            FindingStatus.SUPPRESSED,
            actor="user",
            reason=f"feedback:{chosen.id}:{chosen.kind.value}",
        )
        suppressed += 1
    return suppressed


def matches_file(memory: FeedbackMemory, file_path: str) -> bool:
    """文件级 path/scope 预过滤。无 path/scope 时视为通过。"""
    if not memory.active:
        return False
    if not file_path:
        return False
    normalized = normalize_repo_relpath(file_path)
    if memory.path is not None and memory.path != normalized:
        return False
    return not (memory.scope is not None and not fnmatch.fnmatchcase(normalized, memory.scope))


def should_inject_l4(memory: FeedbackMemory) -> bool:
    """仅 cross_run_match_key 的记忆不进 L4。"""
    others = (
        memory.scope,
        memory.path,
        memory.symbol,
        memory.category,
        memory.rule_key,
        memory.pattern,
    )
    return not (memory.cross_run_match_key and not any(others))


def _l4_sort_key(memory: FeedbackMemory) -> tuple[int, int, int]:
    suppress = 0 if memory.kind in _SUPPRESS_KINDS else 1
    file_cond = 0 if (memory.path or memory.scope) else 1
    return (suppress, file_cond, _suppress_id(memory))


def select_l4_feedback(
    file_path: str,
    memories: Iterable[FeedbackMemory],
    *,
    repo: str,
    limit: int,
) -> tuple[list[FeedbackMemory], list[FeedbackMemory]]:
    """返回 (预算打包前的候选, 因条数上限丢掉的)。"""
    eligible: list[FeedbackMemory] = []
    for memory in memories:
        if not memory.active or memory.repo != repo or not should_inject_l4(memory):
            continue
        if (memory.path or memory.scope) and not matches_file(memory, file_path):
            continue
        eligible.append(memory)
    eligible.sort(key=_l4_sort_key)
    if limit <= 0:
        return [], eligible
    return eligible[:limit], eligible[limit:]


def l4_feedback_text(memory: FeedbackMemory) -> str:
    """程序生成的 L4 文本：不含用户可控路径。"""
    lines = [f"[feedback:{memory.id}] kind={memory.kind.value}"]
    if memory.category:
        lines.append(f"category={memory.category}")
    if memory.rule_key:
        lines.append(f"rule_key={memory.rule_key}")
    if memory.path or memory.scope:
        lines.append("applies to this file by stored path/scope condition")
    rationale = sanitize_rationale(memory.rationale, max_len=RATIONALE_L4_MAX)
    if rationale:
        lines.append(rationale)
    return "\n".join(lines)


def memory_from_finding(
    finding: Finding,
    *,
    kind: FeedbackKind,
    repo: str,
    rationale: str = "",
    with_cross_run_key: bool = False,
    path: str | None = None,
    scope: str | None = None,
    symbol: str | None = None,
    category: str | None = None,
    rule_key: str | None = None,
    pattern: str | None = None,
    cross_run_match_key: str | None = None,
) -> FeedbackMemory:
    """CLI `--from-finding`：默认抄 path/category/rule_id，不抄 cross_run 键。"""
    copied_path = path if path is not None else finding.canonical_path
    copied_category = category if category is not None else finding.category.value
    copied_rule = rule_key if rule_key is not None else finding.rule_id
    copied_cross = cross_run_match_key
    if copied_cross is None and with_cross_run_key:
        copied_cross = finding.cross_run_match_key
    return FeedbackMemory(
        repo=repo,
        kind=kind,
        path=copied_path,
        scope=scope,
        symbol=symbol,
        category=copied_category,
        rule_key=copied_rule,
        pattern=pattern,
        cross_run_match_key=copied_cross,
        rationale=rationale,
    )
