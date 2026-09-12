"""read_diff：读取本 run 已解析 diff，不再 git diff。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from reposage.domain.enums import ToolCallStatus
from reposage.domain.models import ToolDefinition
from reposage.tools.registry import HandlerOutput
from reposage.tools.sandbox import resolve_in_workspace
from reposage.tools.snapshot import ToolWorkspace

MAX_HUNKS = 20
MAX_LINES = 400
MAX_CHARS = 32_000


class ReadDiffArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


DEFINITION = ToolDefinition(
    name="read_diff",
    description="读取指定文件的已解析 diff hunk（含行号映射）。",
    parameters=ReadDiffArgs.model_json_schema(),
    result_limit=MAX_HUNKS,
    timeout_s=10,
    max_result_chars=MAX_CHARS,
)


async def handle(workspace: ToolWorkspace, args: BaseModel) -> HandlerOutput:
    parsed = ReadDiffArgs.model_validate(args.model_dump())
    path = resolve_in_workspace(workspace.repo_root, parsed.path)
    file = workspace.diff_files.get(path)
    if file is None:
        return HandlerOutput(status=ToolCallStatus.ERROR, error="not_found")
    hunks_out: list[dict[str, object]] = []
    line_budget = MAX_LINES
    truncated = False
    for hunk in file.hunks[:MAX_HUNKS]:
        take = hunk.lines[:line_budget]
        if len(hunk.lines) > len(take) or len(file.hunks) > MAX_HUNKS:
            truncated = True
        line_budget -= len(take)
        hunks_out.append(
            {
                "header": hunk.header,
                "new_start": hunk.new_start,
                "lines": [
                    {
                        "type": ln.type.value,
                        "old_ln": ln.old_ln,
                        "new_ln": ln.new_ln,
                        "content": ln.content[:400],
                    }
                    for ln in take
                ],
            }
        )
        if line_budget <= 0:
            truncated = True
            break
    if len(file.hunks) > MAX_HUNKS:
        truncated = True
    return HandlerOutput(
        payload={"path": path, "hunks": hunks_out},
        truncated=truncated,
    )
