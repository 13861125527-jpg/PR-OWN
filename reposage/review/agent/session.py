"""AgentSession 与状态转换（24 §3）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from reposage.domain.enums import EvidenceKind, ReviewTaskStatus
from reposage.domain.finding import FindingCandidate
from reposage.domain.models import (
    AgentBudget,
    AgentMessage,
    AgentSessionSnapshot,
    AgentToolRequest,
    Evidence,
    ToolCall,
    utcnow,
)
from reposage.domain.run import ReviewTask


class AgentStateError(ValueError):
    """非法 AgentTask 状态转换。"""


_ALLOWED: dict[ReviewTaskStatus, frozenset[ReviewTaskStatus]] = {
    ReviewTaskStatus.PENDING: frozenset(
        {ReviewTaskStatus.RUNNING, ReviewTaskStatus.FAILED, ReviewTaskStatus.CANCELLED}
    ),
    ReviewTaskStatus.RUNNING: frozenset(
        {
            ReviewTaskStatus.WAITING_TOOL,
            ReviewTaskStatus.COMPLETED,
            ReviewTaskStatus.PARTIAL,
            ReviewTaskStatus.FAILED,
            ReviewTaskStatus.CANCELLED,
        }
    ),
    ReviewTaskStatus.WAITING_TOOL: frozenset(
        {ReviewTaskStatus.RUNNING, ReviewTaskStatus.FAILED, ReviewTaskStatus.CANCELLED}
    ),
}


def allowed_transition(src: ReviewTaskStatus, dst: ReviewTaskStatus) -> bool:
    return dst in _ALLOWED.get(src, frozenset())


class AgentSession:
    """可变会话；``view()`` 给 Provider 只读快照。"""

    def __init__(
        self,
        task: ReviewTask,
        *,
        file_path: str,
        budget: AgentBudget,
        started_at: datetime | None = None,
    ) -> None:
        self.task = task
        self.session_id = task.task_id
        self.task_id = task.task_id
        self.run_id = task.run_id
        self.file_path = file_path
        self.budget = budget
        self.mode = "exploration"
        self.messages: list[AgentMessage] = []
        self.calls: list[ToolCall] = []
        self.candidates: list[FindingCandidate] = []
        self.evidence_index: dict[str, Evidence] = {}
        self.checked: set[str] = set()
        self.excluded: set[str] = set()
        self.pending: set[str] = set()
        self.compact_seq = 0
        self.idle_count = 0
        self.repeat_count = 0
        self.rounds_used = 0
        self.successful_tools = 0
        self.tool_attempts = 0
        self.grace_rounds_used = 0
        self.json_repair_used = False
        self.json_repair_pending = False
        self.stop_reason = ""
        self.started_at = started_at or utcnow()

    @property
    def status(self) -> ReviewTaskStatus:
        return self.task.status

    def transition(self, dst: ReviewTaskStatus, *, error: str | None = None) -> None:
        src = self.task.status
        if not allowed_transition(src, dst):
            raise AgentStateError(f"illegal transition {src.value} -> {dst.value}")
        self.task.status = dst
        if error is not None:
            self.task.error = error[:300]

    @property
    def evidence_refs(self) -> dict[str, str]:
        return {key: item.location for key, item in self.evidence_index.items()}

    def locating_key(self, name: str, arguments: dict[str, Any] | None) -> str:
        args = arguments or {}
        path = str(args.get("path") or "")
        if name == "read_file" and path:
            start = int(args.get("start_line") or 1)
            span = int(args.get("max_lines") or 1)
            return f"read_file:{path}:L{start}-{start + max(span, 1) - 1}"
        if name == "search_code":
            return f"search_code:{args.get('query') or args.get('pattern') or ''}"
        if name == "find_files":
            return f"find_files:{args.get('pattern') or ''}"
        if name == "find_references":
            return f"find_references:{args.get('symbol') or args.get('name') or ''}"
        if name == "read_diff" and path:
            return f"read_diff:{path}"
        return f"{name}:{path}" if path else name

    def index_success(self, *, tool_call_id: str, name: str, arguments: dict[str, Any], location: str) -> None:
        self.evidence_index[tool_call_id] = Evidence(
            kind=EvidenceKind.TOOL_RESULT,
            location=location,
            content="",
            tool_call_id=tool_call_id,
            verified=True,
        )
        self.checked.add(self.locating_key(name, arguments))
        self.pending.discard(f"{name}:{tool_call_id}")

    def index_excluded(self, *, name: str, arguments: dict[str, Any] | None, error: str) -> None:
        key = self.locating_key(name, arguments)
        self.excluded.add(f"{key}:{error}" if error else key)

    def index_pending(self, *, name: str, tool_call_id: str) -> None:
        self.pending.add(f"{name}:{tool_call_id}")

    def add_system(self, content: str) -> None:
        self.messages.append(AgentMessage(role="system", content=content))

    def add_user(self, content: str) -> None:
        self.messages.append(AgentMessage(role="user", content=content))

    def add_assistant(
        self,
        content: str,
        *,
        tool_calls: list[AgentToolRequest] | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        self.messages.append(
            AgentMessage(
                role="assistant",
                content=content,
                reasoning_content=reasoning_content,
                tool_calls=list(tool_calls or []),
            )
        )

    def add_tool_observation(
        self,
        *,
        tool_call_id: str,
        name: str,
        content: str,
    ) -> None:
        self.messages.append(
            AgentMessage(
                role="tool",
                content=content,
                name=name,
                tool_call_id=tool_call_id,
            )
        )

    def message_chars(self) -> int:
        return sum(len(m.content) for m in self.messages)

    def remaining_rounds(self) -> int:
        return max(0, self.budget.max_rounds - self.rounds_used)

    def enter_grace(self) -> bool:
        """进入 grace 一次。已在 grace 则返回 False，避免重复日志。"""
        if self.mode == "grace":
            return False
        self.mode = "grace"
        self.grace_rounds_used = 0
        return True

    def grace_rounds_exhausted(self) -> bool:
        if self.mode != "grace":
            return False
        return self.budget.grace_rounds <= 0 or self.grace_rounds_used >= self.budget.grace_rounds

    def should_enter_grace(self) -> bool:
        if self.mode == "grace" or self.json_repair_pending:
            return False
        if self.successful_tools >= self.budget.max_tool_calls:
            return True
        if self.tool_attempts >= self.budget.max_tool_attempts:
            return True
        explore_cap = max(0, self.budget.max_rounds - self.budget.grace_rounds)
        return self.rounds_used >= explore_cap

    def wallclock_expired(self, *, global_remaining_s: float) -> bool:
        elapsed = (utcnow() - self.started_at).total_seconds()
        return elapsed >= self.budget.max_wallclock_s or global_remaining_s <= 0

    def view(self) -> AgentSessionSnapshot:
        return AgentSessionSnapshot(
            session_id=self.session_id,
            mode=self.mode,
            messages=tuple(self.messages),
            remaining_rounds_value=self.remaining_rounds(),
            budget_summary_value={
                "mode": self.mode,
                "rounds_used": self.rounds_used,
                "max_rounds": self.budget.max_rounds,
                "successful_tools": self.successful_tools,
                "max_tool_calls": self.budget.max_tool_calls,
                "tool_attempts": self.tool_attempts,
                "max_tool_attempts": self.budget.max_tool_attempts,
                "grace_rounds_used": self.grace_rounds_used,
                "json_repair_used": self.json_repair_used,
            },
        )
