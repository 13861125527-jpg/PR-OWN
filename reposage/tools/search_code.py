"""search_code：字面量子串搜索（DP-18）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from reposage.domain.models import ToolDefinition
from reposage.tools.registry import HandlerOutput
from reposage.tools.sandbox import glob_is_safe, match_repo_glob
from reposage.tools.snapshot import ToolWorkspace

MAX_HITS = 50
MAX_CHARS = 24_000
MAX_FILES_SCANNED = 100
MAX_CHARS_PER_FILE = 4_000
MAX_TOTAL_CHARS_SCANNED = 200_000


class SearchCodeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=256)
    scope: str | None = Field(default=None, max_length=256)
    case_sensitive: bool = False


DEFINITION = ToolDefinition(
    name="search_code",
    description="在锁定 head 快照中按字面量子串搜索代码。",
    parameters=SearchCodeArgs.model_json_schema(),
    result_limit=MAX_HITS,
    timeout_s=15,
    max_result_chars=MAX_CHARS,
)


async def handle(workspace: ToolWorkspace, args: BaseModel) -> HandlerOutput:
    parsed = SearchCodeArgs.model_validate(args.model_dump())
    if parsed.scope is not None:
        glob_is_safe(parsed.scope)
    needle = parsed.query if parsed.case_sensitive else parsed.query.lower()
    hits: list[dict[str, object]] = []
    files_scanned = 0
    total_chars = 0
    truncated = False
    for path in await workspace.snapshot.list_paths():
        if parsed.scope is not None and not match_repo_glob(path, parsed.scope):
            continue
        if files_scanned >= MAX_FILES_SCANNED or total_chars >= MAX_TOTAL_CHARS_SCANNED:
            truncated = True
            break
        blob = await workspace.snapshot.get_blob(path)
        if blob is None:
            continue
        files_scanned += 1
        snippet = blob[:MAX_CHARS_PER_FILE]
        if len(blob) > MAX_CHARS_PER_FILE:
            truncated = True
        total_chars += len(snippet)
        if total_chars > MAX_TOTAL_CHARS_SCANNED:
            truncated = True
            keep = max(0, len(snippet) - (total_chars - MAX_TOTAL_CHARS_SCANNED))
            snippet = snippet[:keep]
        for lineno, line in enumerate(snippet.splitlines(), start=1):
            hay_line = line if parsed.case_sensitive else line.lower()
            if needle not in hay_line:
                continue
            hits.append({"path": path, "line": lineno, "text": line[:400]})
            if len(hits) >= MAX_HITS:
                return HandlerOutput(payload={"hits": hits}, truncated=True)
        if total_chars >= MAX_TOTAL_CHARS_SCANNED:
            truncated = True
            break
    return HandlerOutput(payload={"hits": hits}, truncated=truncated)
