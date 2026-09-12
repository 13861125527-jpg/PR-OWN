"""finish_review stub：V3-A 只注册，不结束 Agent 任务。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from reposage.domain.enums import ToolCallStatus
from reposage.domain.models import ToolDefinition
from reposage.tools.registry import HandlerOutput
from reposage.tools.snapshot import ToolWorkspace


class FinishReviewArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=200)


DEFINITION = ToolDefinition(
    name="finish_review",
    description="结束本任务审查，进入终局验证（V3-B；V3-A 未绑定）。",
    parameters=FinishReviewArgs.model_json_schema(),
    result_limit=0,
    timeout_s=5,
    max_result_chars=512,
)


async def handle(workspace: ToolWorkspace, args: BaseModel) -> HandlerOutput:
    del workspace, args
    return HandlerOutput(status=ToolCallStatus.ERROR, error="not_bound")
