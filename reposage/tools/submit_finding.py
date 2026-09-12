"""submit_finding stub：V3-A 只注册，不进 Pipeline。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from reposage.domain.enums import FindingCategory, Severity, ToolCallStatus
from reposage.domain.models import ToolDefinition
from reposage.tools.registry import HandlerOutput
from reposage.tools.snapshot import ToolWorkspace


class SubmitFindingArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    category: FindingCategory
    claimed_path: str | None = None
    claimed_start_line: int | None = Field(default=None, ge=1)
    claimed_end_line: int | None = Field(default=None, ge=1)
    explanation: str = Field(default="", max_length=2000)
    suggestion: str = Field(default="", max_length=2000)
    trigger_condition: str = Field(default="", max_length=500)
    evidence_tool_call_ids: list[str] = Field(default_factory=list, max_length=8)


DEFINITION = ToolDefinition(
    name="submit_finding",
    description="提交一条候选 Finding（V3-B 接入流水线；V3-A 未绑定）。",
    parameters=SubmitFindingArgs.model_json_schema(),
    result_limit=0,
    timeout_s=5,
    max_result_chars=512,
)


async def handle(workspace: ToolWorkspace, args: BaseModel) -> HandlerOutput:
    del workspace, args
    return HandlerOutput(status=ToolCallStatus.ERROR, error="not_bound")
