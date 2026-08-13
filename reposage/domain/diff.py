"""Unified diff 解析与文件过滤（domain/diff.py）。

V1-a 任务卡（12 §2）：diff 解析器 + 行号映射 + 文件过滤。
纯函数，无 IO；契约来源：04 §1/§2、05 §2（ChangedFile/DiffHunk/DiffLine）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .enums import ChangedFileStatus, CoverageReason, DiffLineType
from .models import ChangedFile, DiffHunk, DiffLine

# ---- 正则（git diff 输出格式） ----

_FILE_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)$")
_INDEX_LINE = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+")
_NEW_FILE = re.compile(r"^new file mode ")
_DELETED_FILE = re.compile(r"^deleted file mode ")
_RENAME_FROM = re.compile(r"^rename from (.*)$")
_RENAME_TO = re.compile(r"^rename to (.*)$")
_SIMILARITY = re.compile(r"^similarity index (\d+)%")
_BINARY = re.compile(r"^Binary files .* differ$")
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_OLD_FILE = re.compile(r"^--- (.*)$")
_NEW_FILE_HEADER = re.compile(r"^\+\+\+ (.*)$")

# 生成文件 / lock / 二进制识别（可配置扩展）
_GENERATED_PATTERNS = (
    re.compile(r"(^|/)(__pycache__|dist|build|node_modules|\.venv|venv|target)/"),
    re.compile(r"\.(pyc|pyo|min\.js|map)$"),
    re.compile(r"(_pb2\.py|_pb2_grpc\.py|generated_.*\.py|\.g\.py)$"),
)
_LOCK_FILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "go.sum",
}
_BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".whl",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".class",
    ".jar",
    ".woff",
    ".woff2",
    ".ttf",
}
# 首发语言（05 §1 / 09 §6 review.languages）
_LANGUAGE_EXTENSIONS: dict[str, set[str]] = {"python": {".py", ".pyi"}}


class DiffParseError(ValueError):
    """diff 文本无法解析为合法结构。"""


@dataclass
class ParseResult:
    """解析结果：文件列表 + 未识别块（用于审计）。"""

    files: list[ChangedFile] = field(default_factory=list)
    skipped_blocks: list[str] = field(default_factory=list)


def _detect_language(path: str) -> str | None:
    lower = path.lower()
    for lang, exts in _LANGUAGE_EXTENSIONS.items():
        if any(lower.endswith(ext) for ext in exts):
            return lang
    return None


def parse_unified_diff(text: str) -> list[ChangedFile]:
    """把 unified diff 文本解析为 ChangedFile 列表（含 hunk 与行号映射）。"""
    return _parse(text).files


def _parse(text: str) -> ParseResult:
    if not text.strip():
        return ParseResult()

    result = ParseResult()
    lines = text.splitlines()

    current: ChangedFile | None = None
    current_hunk: DiffHunk | None = None
    old_ln: int = 0
    new_ln: int = 0
    pending: list[str] = []  # 未归属的原始行

    def flush_file() -> None:
        nonlocal current
        if current is not None:
            result.files.append(current)
        current = None

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        m = _FILE_HEADER.match(line)
        if m:
            flush_file()
            current = ChangedFile(path=m.group(2), status=ChangedFileStatus.MODIFIED)
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
            current.old_path = m.group(1)
            current.status = ChangedFileStatus.RENAMED
            i += 1
            continue
        m = _RENAME_TO.match(line)
        if m:
            current.path = m.group(1)
            i += 1
            continue
        if _INDEX_LINE.match(line) or _SIMILARITY.match(line):
            i += 1
            continue

        m = _HUNK_HEADER.match(line)
        if m:
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
                i += 1  # 行尾标记，不计数
                continue
            if line.startswith(" "):
                dline = DiffLine(type=DiffLineType.CONTEXT, old_ln=old_ln, new_ln=new_ln, content=line[1:])
                old_ln += 1
                new_ln += 1
            elif line.startswith("-"):
                dline = DiffLine(type=DiffLineType.REMOVED, old_ln=old_ln, content=line[1:])
                old_ln += 1
            elif line.startswith("+"):
                dline = DiffLine(type=DiffLineType.ADDED, new_ln=new_ln, content=line[1:])
                new_ln += 1
            else:
                raise DiffParseError(f"无法识别的 hunk 行: {line!r}")
            current_hunk.lines.append(dline)
            i += 1
            continue

        pending.append(line)
        i += 1

    flush_file()
    result.skipped_blocks = pending
    return result


# ---- 文件过滤（V1-a） ----


@dataclass
class FileFilterResult:
    """过滤结果（skipped 直接可转 CoverageItem，07 §8）。"""

    kept: list[ChangedFile]
    skipped: list[tuple[ChangedFile, CoverageReason, str]] = field(default_factory=list)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


def _is_generated(path: str, status: ChangedFileStatus) -> bool:
    lower = path.lower()
    for pat in _GENERATED_PATTERNS:
        if pat.search(lower):
            return True
    return path.split("/")[-1] in _LOCK_FILES


def _is_binary_file(path: str) -> bool:
    return any(path.lower().endswith(ext) for ext in _BINARY_EXTENSIONS)


def filter_files(
    files: list[ChangedFile],
    *,
    languages: list[str] | None = None,
    max_files: int = 40,
    max_added_lines: int = 2000,
) -> FileFilterResult:
    """按 04 §2 / 07 §8 规则过滤。

    - 生成文件 / lock 文件 → skipped_generated
    - 二进制（显式标记或扩展名）→ skipped_lang(detail=binary)
    - 超大（additions+deletions 超过 max_added_lines，V1-a 用行数近似字节）→ skipped_size
    - 语言不支持 → skipped_lang
    - 文件总数超过 max_files → 后续按优先级截断（由调用方处理，此处只标记超限）
    - deleted/renamed 文件保留（04 §7b：无行内锚点，走 body_only/摘要）
    """
    languages = languages or ["python"]
    kept: list[ChangedFile] = []
    skipped: list[tuple[ChangedFile, CoverageReason, str]] = []

    for f in files:
        if f.is_binary or _is_binary_file(f.path):
            skipped.append((f, CoverageReason.SKIPPED_LANG, "binary"))
            continue
        if f.status is ChangedFileStatus.DELETED:
            # 保留：删除无新增行，无行内锚点（04 §7b）
            kept.append(f)
            continue
        if _is_generated(f.path, f.status):
            skipped.append((f, CoverageReason.SKIPPED_GENERATED, "generated/lockfile"))
            continue
        lang = _detect_language(f.path)
        if lang is None or (languages and lang not in languages):
            # 未知语言或未启用语言均跳过（01 FR-5：不支持语言过滤）
            skipped.append((f, CoverageReason.SKIPPED_LANG, f"language:{lang or 'unknown'}"))
            continue
        if f.additions + f.deletions > max_added_lines:
            skipped.append((f, CoverageReason.SKIPPED_SIZE, "too many diff lines"))
            continue
        kept.append(f)

    if len(kept) > max_files:
        # 07 §8：超大 PR 降级由调用方按优先级截断；此处标记
        excess = kept[max_files:]
        for f in excess:
            skipped.append((f, CoverageReason.SKIPPED_SIZE, "max_files exceeded"))
        kept = kept[:max_files]

    return FileFilterResult(kept=kept, skipped=skipped)
