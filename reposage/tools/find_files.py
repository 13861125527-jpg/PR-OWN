"""find_files：在锁定 SHA 路径集合上做 glob。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from reposage.domain.models import ToolDefinition
from reposage.tools.registry import HandlerOutput
from reposage.tools.sandbox import glob_is_safe, match_repo_glob
from reposage.tools.snapshot import ToolWorkspace

MAX_HITS = 100
MAX_CHARS = 16_000


class FindFilesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(min_length=1, max_length=256)


DEFINITION = ToolDefinition(
    name="find_files",
    description="按名称/glob 在锁定 head 快照中找文件（仓库根内）。",
    parameters=FindFilesArgs.model_json_schema(),
    result_limit=MAX_HITS,
    timeout_s=10,
    max_result_chars=MAX_CHARS,
)


async def handle(workspace: ToolWorkspace, args: BaseModel) -> HandlerOutput:
    parsed = FindFilesArgs.model_validate(args.model_dump())
    glob_is_safe(parsed.pattern)
    matches: list[str] = []
    truncated = False
    for path in await workspace.snapshot.list_paths():
        if not match_repo_glob(path, parsed.pattern):
            continue
        if len(matches) >= MAX_HITS:
            truncated = True
            break
        matches.append(path)
    return HandlerOutput(payload={"paths": matches}, truncated=truncated)
