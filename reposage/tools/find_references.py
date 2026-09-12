"""find_references：复用 V2-B 符号抽取。"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reposage.domain.enums import ToolCallStatus
from reposage.domain.models import ToolDefinition
from reposage.review.symbols.extract import SymbolIndex
from reposage.review.symbols.snapshot import is_python_path
from reposage.tools.registry import HandlerOutput
from reposage.tools.sandbox import resolve_in_workspace
from reposage.tools.snapshot import ToolWorkspace

MAX_HITS = 50
MAX_CHARS = 24_000
MAX_FILES = 100
MAX_CHARS_PER_FILE = 16_000
MAX_TOTAL_CHARS_SCANNED = 200_000
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WORD = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


class FindReferencesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=128)
    scope: Literal["repo", "file"] = "repo"
    path: str | None = None

    @model_validator(mode="after")
    def _file_needs_path(self) -> FindReferencesArgs:
        if self.scope == "file" and not self.path:
            raise ValueError("scope=file requires path")
        return self


DEFINITION = ToolDefinition(
    name="find_references",
    description="查找符号在仓库中的定义与引用位置（结果含文件+行+截断片段）。",
    parameters=FindReferencesArgs.model_json_schema(),
    result_limit=MAX_HITS,
    timeout_s=15,
    max_result_chars=MAX_CHARS,
)


async def handle(workspace: ToolWorkspace, args: BaseModel) -> HandlerOutput:
    parsed = FindReferencesArgs.model_validate(args.model_dump())
    if not _IDENT.fullmatch(parsed.symbol):
        return HandlerOutput(payload={"refs": []})
    index = workspace.symbol_index or SymbolIndex()
    if parsed.scope == "file":
        if parsed.path is None:
            return HandlerOutput(status=ToolCallStatus.INVALID_ARGS, error="invalid_path")
        paths = [resolve_in_workspace(workspace.repo_root, parsed.path)]
    else:
        paths = sorted(p for p in await workspace.snapshot.list_paths() if is_python_path(p))
    refs: list[dict[str, object]] = []
    truncated = False
    scanned = 0
    total_chars = 0
    for path in paths:
        if scanned >= MAX_FILES or total_chars >= MAX_TOTAL_CHARS_SCANNED:
            truncated = True
            break
        blob = await workspace.snapshot.get_blob(path)
        if blob is None:
            continue
        scanned += 1
        snippet = blob[:MAX_CHARS_PER_FILE]
        if len(blob) > MAX_CHARS_PER_FILE:
            truncated = True
        remain = MAX_TOTAL_CHARS_SCANNED - total_chars
        if remain <= 0:
            truncated = True
            break
        if len(snippet) > remain:
            snippet = snippet[:remain]
            truncated = True
        total_chars += len(snippet)
        defs = index.defs_for(
            path,
            snippet,
            repository_id=workspace.snapshot.repository_id,
            head_sha=workspace.snapshot.head_sha,
        )
        seen_lines: set[int] = set()
        for defn in defs:
            if defn.name != parsed.symbol:
                continue
            snippet_line = defn.signature or _line_at(snippet, defn.start_line)
            refs.append(
                {
                    "path": path,
                    "line": defn.start_line,
                    "name": defn.name,
                    "kind": defn.kind.value,
                    "snippet": snippet_line[:200],
                }
            )
            seen_lines.add(defn.start_line)
            if len(refs) >= MAX_HITS:
                return HandlerOutput(payload={"refs": refs}, truncated=True)
        for lineno, line in enumerate(snippet.splitlines(), start=1):
            if lineno in seen_lines:
                continue
            if parsed.symbol not in line:
                continue
            if parsed.symbol not in _WORD.findall(line):
                continue
            refs.append(
                {
                    "path": path,
                    "line": lineno,
                    "name": parsed.symbol,
                    "kind": "ref",
                    "snippet": line.strip()[:200],
                }
            )
            if len(refs) >= MAX_HITS:
                return HandlerOutput(payload={"refs": refs}, truncated=True)
        if total_chars >= MAX_TOTAL_CHARS_SCANNED:
            truncated = True
            break
    return HandlerOutput(payload={"refs": refs}, truncated=truncated)


def _line_at(source: str, lineno: int) -> str:
    lines = source.splitlines()
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()[:200]
    return ""
