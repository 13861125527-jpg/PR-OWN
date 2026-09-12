"""read_file：锁定 SHA 快照按行读取。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from reposage.domain.enums import ToolCallStatus
from reposage.domain.models import ToolDefinition
from reposage.tools.registry import HandlerOutput
from reposage.tools.sandbox import resolve_in_workspace
from reposage.tools.snapshot import ToolWorkspace

MAX_LINES = 200
MAX_CHARS = 16_000


class ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=MAX_LINES, ge=1, le=MAX_LINES)


DEFINITION = ToolDefinition(
    name="read_file",
    description="读取仓库内文件指定行区间，用于验证上下文。",
    parameters=ReadFileArgs.model_json_schema(),
    result_limit=MAX_LINES,
    timeout_s=10,
    max_result_chars=MAX_CHARS,
)


async def handle(workspace: ToolWorkspace, args: BaseModel) -> HandlerOutput:
    parsed = ReadFileArgs.model_validate(args.model_dump())
    path = resolve_in_workspace(workspace.repo_root, parsed.path)
    blob = await workspace.snapshot.get_blob(path)
    if blob is None:
        return HandlerOutput(status=ToolCallStatus.ERROR, error="not_found")
    lines = blob.splitlines()
    start = parsed.start_line
    sliced = lines[start - 1 : start - 1 + parsed.max_lines]
    truncated = (start - 1 + len(sliced)) < len(lines)
    return HandlerOutput(
        payload={"path": path, "start_line": start, "lines": sliced},
        truncated=truncated,
    )
