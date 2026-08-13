"""Unified diff 解析与文件过滤（domain/diff.py）。

V1-a 任务卡（12 §2）：diff 解析器 + 行号映射 + 文件过滤。
纯函数，无 IO；契约来源：04 §1/§2、05 §2（ChangedFile/DiffHunk/DiffLine）、01 FR-5。

V1-a 返工（2026-08）：
- P1-1：解析时累计文件级 additions/deletions（多 hunk 合计；context 与行尾标记不计）
- P1-2：支持 Git quoted/escaped path（统一 _unquote_git_path；无法解码抛 DiffParseError）
- P2-1：hunk 声明行数与实际正文一致性校验
- P2-2：删除状态只影响评论锚点策略，不绕过 generated/language/size 基础过滤
- P2-3：FilterRules 可配置（domain 不读环境/配置文件）
- P3-1：parse 填 language/is_binary，filter 对 kept 回填 is_generated/is_locked
- P3-2：max_added_lines → max_diff_lines
- P3-3：路径安全校验覆盖反斜杠/盘符/UNC（models.py validator）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .enums import ChangedFileStatus, CoverageReason, DiffLineType
from .models import ChangedFile, DiffHunk, DiffLine

# ---- 正则（git diff 输出格式） ----

_DIFF_GIT = re.compile(r"^diff --git (.*)$")
_INDEX_LINE = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+")
_NEW_FILE = re.compile(r"^new file mode ")
_DELETED_FILE = re.compile(r"^deleted file mode ")
_RENAME_FROM = re.compile(r"^rename from (.*)$")
_RENAME_TO = re.compile(r"^rename to (.*)$")
_SIMILARITY = re.compile(r"^similarity index \d+%")
_BINARY = re.compile(r"^Binary files .* differ$")
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_OLD_FILE = re.compile(r"^--- (.*)$")
_NEW_FILE_HEADER = re.compile(r"^\+\+\+ (.*)$")
_MODE_LINE = re.compile(r"^(old mode|new mode) \d+$")


class DiffParseError(ValueError):
    """diff 文本无法解析为合法结构。"""


# ---- Git quoted/escaped path（P1-2） ----


def _unquote_git_path(s: str) -> str:
    """还原 Git quoted path（core.quotepath 输出）。

    - 非引号路径原样返回；
    - 引号内支持 \\" \\\\ \\t \\n \\r 与 \\ooo（八进制，Git 以 UTF-8 字节转义非 ASCII）；
      连续八进制按字节收集后统一 UTF-8 解码。
    无法解码时抛 DiffParseError。
    """
    if not (s.startswith('"') and s.endswith('"')):
        return s
    inner = s[1:-1]
    out = bytearray()
    i = 0
    n = len(inner)
    while i < n:
        c = inner[i]
        if c != "\\":
            out.extend(c.encode("utf-8"))
            i += 1
            continue
        if i + 1 >= n:
            raise DiffParseError(f"非法路径转义（截断）: {s!r}")
        nxt = inner[i + 1]
        if nxt == '"':
            out.append(0x22)
            i += 2
        elif nxt == "\\":
            out.append(0x5C)
            i += 2
        elif nxt == "t":
            out.append(0x09)
            i += 2
        elif nxt == "n":
            out.append(0x0A)
            i += 2
        elif nxt == "r":
            out.append(0x0D)
            i += 2
        elif nxt in "01234567":
            digits = inner[i + 1 : i + 4]
            if len(digits) != 3 or any(d not in "01234567" for d in digits):
                # P2（复验）：\128 等含 8/9 的"八进制"也必须抛 DiffParseError，而非 int() 的 ValueError
                raise DiffParseError(f"非法路径转义（八进制需 3 位 0-7）: {s!r}")
            out.append(int(digits, 8))
            i += 4
        else:
            raise DiffParseError(f"未知路径转义: {s!r}")
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError as e:
        raise DiffParseError(f"非法 UTF-8 路径转义: {s!r}") from e


def _strip_ab_prefix(raw: str, prefix: str) -> str:
    """去掉 a/ 或 b/ 前缀（raw 可能为 quoted 形式）。"""
    path = _unquote_git_path(raw)
    if path.startswith(prefix + "/"):
        path = path[len(prefix) + 1 :]
    return path


def _split_ab(spec: str) -> tuple[str, str]:
    """拆分 diff --git 的 'a/X b/Y'（X/Y 可能 quoted 且含空格）。

    遍历时跟踪引号状态；反斜杠转义（如 \\"）不改变引号状态；
    取第一个不在引号内的分隔：空格后紧跟（可选引号）'b/'。
    """
    in_quote = False
    i = 0
    n = len(spec)
    while i < n:
        c = spec[i]
        if c == "\\":
            i += 2  # 跳过转义字符及其后一个字符
            continue
        if c == '"':
            in_quote = not in_quote
            i += 1
            continue
        if not in_quote and c == " ":
            j = i + 1
            if j < n and spec[j] == '"':
                j += 1
            if spec[j : j + 2] == "b/":
                # a 侧 = spec[:i]（可能为 "a/xxx"），b 侧 = spec[i+1:]（可能为 "b/xxx"）
                return _strip_ab_prefix(spec[:i], "a"), _strip_ab_prefix(spec[i + 1 :], "b")
        i += 1
    raise DiffParseError(f"无法解析 diff --git 头: {spec!r}")


# ---- 默认过滤规则（P2-3：可配置） ----


class FilterRules(BaseModel):
    """过滤规则配置（domain 层对象；由 application 层从 Settings/文件构造传入）。

    generated_patterns 为正则字符串；lock_files / binary_extensions 为字面集合；
    language_extensions 为 {语言: [扩展名]}。
    """

    generated_patterns: list[str] = Field(
        default_factory=lambda: [
            r"(^|/)(__pycache__|dist|build|node_modules|\.venv|venv|target)/",
            r"\.(pyc|pyo|min\.js|map)$",
            r"(_pb2\.py|_pb2_grpc\.py|generated_.*\.py|\.g\.py)$",
        ]
    )
    lock_files: set[str] = Field(
        default_factory=lambda: {
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "poetry.lock",
            "Pipfile.lock",
            "Cargo.lock",
            "Gemfile.lock",
            "go.sum",
        }
    )
    binary_extensions: set[str] = Field(
        default_factory=lambda: {
            ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz",
            ".tar", ".whl", ".exe", ".dll", ".so", ".dylib", ".class", ".jar",
            ".woff", ".woff2", ".ttf",
        }
    )
    language_extensions: dict[str, list[str]] = Field(
        default_factory=lambda: {"python": [".py", ".pyi"]}
    )


DEFAULT_RULES = FilterRules()


@dataclass
class ParseResult:
    """解析结果：文件列表 + 未识别块（用于审计）。"""

    files: list[ChangedFile] = field(default_factory=list)
    skipped_blocks: list[str] = field(default_factory=list)


def _detect_language(path: str, rules: FilterRules = DEFAULT_RULES) -> str | None:
    lower = path.lower()
    for lang, exts in rules.language_extensions.items():
        if any(lower.endswith(ext) for ext in exts):
            return lang
    return None


def parse_unified_diff(text: str) -> list[ChangedFile]:
    """把 unified diff 文本解析为 ChangedFile 列表（含 hunk、行号映射与增删统计）。"""
    return _parse(text).files


def _validate_hunk_counts(hunk: DiffHunk, path: str) -> None:
    """P2-1：hunk 声明行数与实际正文一致性校验。"""
    old_actual = sum(1 for ln in hunk.lines if ln.type in (DiffLineType.CONTEXT, DiffLineType.REMOVED))
    new_actual = sum(1 for ln in hunk.lines if ln.type in (DiffLineType.CONTEXT, DiffLineType.ADDED))
    if old_actual != hunk.old_count or new_actual != hunk.new_count:
        raise DiffParseError(
            f"hunk 行数不一致（{path}，{hunk.header!r}）："
            f"声明旧 {hunk.old_count}/新 {hunk.new_count}，实际旧 {old_actual}/新 {new_actual}"
        )


def _parse(text: str) -> ParseResult:
    if not text.strip():
        return ParseResult()

    result = ParseResult()
    lines = text.splitlines()

    current: ChangedFile | None = None
    current_hunk: DiffHunk | None = None
    old_ln: int = 0
    new_ln: int = 0
    pending: list[str] = []

    def flush_file() -> None:
        nonlocal current
        if current is not None:
            for hunk in current.hunks:
                _validate_hunk_counts(hunk, current.path)
            result.files.append(current)
        current = None

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        m = _DIFF_GIT.match(line)
        if m:
            flush_file()
            old_p, new_p = _split_ab(m.group(1))
            current = ChangedFile(
                path=new_p,
                old_path=old_p if old_p != new_p else None,
                status=ChangedFileStatus.MODIFIED,
                language=_detect_language(new_p),
            )
            current_hunk = None
            i += 1
            continue

        if current is None:
            pending.append(line)
            i += 1
            continue

        if _BINARY.match(line):
            current.is_binary = True
            i += 1
            continue
        if _NEW_FILE.match(line):
            current.status = ChangedFileStatus.ADDED
            i += 1
            continue
        if _DELETED_FILE.match(line):
            current.status = ChangedFileStatus.DELETED
            i += 1
            continue
        m = _RENAME_FROM.match(line)
        if m:
            current.old_path = _strip_ab_prefix(m.group(1), "a")
            current.status = ChangedFileStatus.RENAMED
            i += 1
            continue
        m = _RENAME_TO.match(line)
        if m:
            current.path = _strip_ab_prefix(m.group(1), "b")
            i += 1
            continue
        if _INDEX_LINE.match(line) or _SIMILARITY.match(line) or _MODE_LINE.match(line):
            i += 1
            continue

        m = _OLD_FILE.match(line)
        if m:
            i += 1
            continue  # --- a/x 已由 diff --git 提供；保留兼容但不再依赖
        m = _NEW_FILE_HEADER.match(line)
        if m:
            i += 1
            continue

        m = _HUNK_HEADER.match(line)
        if m:
            if current_hunk is not None:
                _validate_hunk_counts(current_hunk, current.path)
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) else 1
            current_hunk = DiffHunk(
                hunk_id=f"{current.path}:{len(current.hunks) + 1}",
                header=line,
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
            )
            current.hunks.append(current_hunk)
            old_ln = old_start
            new_ln = new_start
            i += 1
            continue

        # hunk 体：context / removed / added / "\ No newline at end of file"
        if current_hunk is not None:
            if line.startswith("\\"):
                i += 1  # 行尾标记，不计数（P1-1）
                continue
            if line.startswith(" "):
                dline = DiffLine(type=DiffLineType.CONTEXT, old_ln=old_ln, new_ln=new_ln, content=line[1:])
                old_ln += 1
                new_ln += 1
            elif line.startswith("-"):
                dline = DiffLine(type=DiffLineType.REMOVED, old_ln=old_ln, content=line[1:])
                current.deletions += 1  # P1-1
                old_ln += 1
            elif line.startswith("+"):
                dline = DiffLine(type=DiffLineType.ADDED, new_ln=new_ln, content=line[1:])
                current.additions += 1  # P1-1
                new_ln += 1
            else:
                raise DiffParseError(f"无法识别的 hunk 行: {line!r}")
            current_hunk.lines.append(dline)
            i += 1
            continue

        pending.append(line)
        i += 1

    if current_hunk is not None:
        _validate_hunk_counts(current_hunk, current.path if current else "<unknown>")
    flush_file()
    result.skipped_blocks = pending
    return result


# ---- 文件过滤（V1-a，P2-2/P2-3） ----


@dataclass
class FileFilterResult:
    """过滤结果（skipped 直接可转 CoverageItem，07 §8）。"""

    kept: list[ChangedFile]
    skipped: list[tuple[ChangedFile, CoverageReason, str]] = field(default_factory=list)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


def _is_generated(path: str, rules: FilterRules) -> bool:
    lower = path.lower()
    for pat in (re.compile(p) for p in rules.generated_patterns):
        if pat.search(lower):
            return True
    return path.split("/")[-1] in rules.lock_files


def _is_binary_file(path: str, rules: FilterRules) -> bool:
    return any(path.lower().endswith(ext) for ext in rules.binary_extensions)


def filter_files(
    files: list[ChangedFile],
    *,
    rules: FilterRules | None = None,
    languages: list[str] | None = None,
    max_files: int = 40,
    max_diff_lines: int = 2000,
) -> FileFilterResult:
    """按 04 §2 / 07 §8 规则过滤（P2-2：删除状态不绕过基础过滤）。

    - 二进制（显式标记或扩展名）→ skipped_lang(detail=binary)
    - 生成文件 / lock 文件 → skipped_generated
    - 未知语言或未启用语言 → skipped_lang（01 FR-5）
    - 超大（additions+deletions 超 max_diff_lines）→ skipped_size
    - 文件总数超 max_files → 按序截断（07 §8 由调用方按优先级排序前调用）
    - 保留的文件（含 deleted/renamed）回填元数据（P3-1）；
      deleted 无行内锚点由下游 body_only/摘要处理（04 §7b），不在此绕过过滤。
    """
    rules = rules or DEFAULT_RULES
    languages = languages or ["python"]
    kept: list[ChangedFile] = []
    skipped: list[tuple[ChangedFile, CoverageReason, str]] = []

    for f in files:
        if f.is_binary or _is_binary_file(f.path, rules):
            skipped.append((f, CoverageReason.SKIPPED_LANG, "binary"))
            continue
        if _is_generated(f.path, rules):
            skipped.append((f, CoverageReason.SKIPPED_GENERATED, "generated/lockfile"))
            continue
        lang = _detect_language(f.path, rules)
        if lang is None or (languages and lang not in languages):
            skipped.append((f, CoverageReason.SKIPPED_LANG, f"language:{lang or 'unknown'}"))
            continue
        if f.additions + f.deletions > max_diff_lines:
            skipped.append((f, CoverageReason.SKIPPED_SIZE, "too many diff lines"))
            continue
        kept.append(f)

    # 回填元数据（P3-1）：is_generated / is_locked / is_binary（扩展名）/ language
    enriched: list[ChangedFile] = []
    for f in kept:
        lang = _detect_language(f.path, rules)
        enriched.append(
            f.model_copy(
                update={
                    "language": lang,
                    "is_generated": _is_generated(f.path, rules),
                    "is_locked": f.path.split("/")[-1] in rules.lock_files,
                    "is_binary": f.is_binary or _is_binary_file(f.path, rules),
                }
            )
        )
    kept = enriched

    if len(kept) > max_files:
        # 07 §8：超大 PR 降级由调用方按优先级截断；此处标记
        excess = kept[max_files:]
        for f in excess:
            skipped.append((f, CoverageReason.SKIPPED_SIZE, "max_files exceeded"))
        kept = kept[:max_files]

    return FileFilterResult(kept=kept, skipped=skipped)
