"""V3-C 会话压缩与证据索引。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from reposage.domain.enums import FindingSourceKind, ReviewTaskKind, ReviewTaskStatus
from reposage.domain.models import AgentBudget, AgentToolRequest, GlobalBudget
from reposage.domain.protocols import ModelResponse
from reposage.domain.run import ReviewRun, ReviewTask
from reposage.providers.git.fake import FakeGitProvider
from reposage.providers.llm.agent_chat import messages_to_chat
from reposage.providers.llm.fake import FakeLLMProvider
from reposage.review.agent.budget import FinalizeBudgetCoordinator
from reposage.review.agent.compact import JSON_REPAIR_HINT, maybe_compact
from reposage.review.agent.loop import run_agent_loop
from reposage.review.agent.session import AgentSession
from reposage.review.service import ReviewService
from reposage.storage.sqlite import SqliteStorage
from reposage.tools import builtin_registry
from reposage.tools.snapshot import MemoryToolSnapshot, ToolWorkspace


def _task() -> ReviewTask:
    return ReviewTask(
        task_id="run-a:src/a.py:agent",
        run_id="run-a",
        kind=ReviewTaskKind.AGENT_TASK,
        target="src/a.py",
    )


def _session() -> AgentSession:
    budget = AgentBudget(
        max_rounds=8,
        compact_threshold_ratio=0.4,
        compact_keep_rounds=1,
        max_session_chars=1500,
    )
    return AgentSession(_task(), file_path="src/a.py", budget=budget)


def _resp(name: str, args: dict[str, object], call_id: str) -> ModelResponse:
    action = name if name in {"submit_finding", "finish_review"} else "tool_call"
    req = AgentToolRequest(id=call_id, name=name, arguments=args)
    return ModelResponse(action=action, data={"name": name, "arguments": args}, tool_requests=[req])


def _add_round(session: AgentSession, call_id: str, payload: str) -> None:
    session.add_assistant(
        "",
        tool_calls=[AgentToolRequest(id=call_id, name="read_file", arguments={"path": "src/a.py", "start_line": 1})],
    )
    session.add_tool_observation(tool_call_id=call_id, name="read_file", content=payload)


def _pairing_ok(session: AgentSession) -> bool:
    chat = messages_to_chat(session.messages, protocol="native")
    open_ids: set[str] = set()
    for message in chat:
        if message.get("role") == "assistant":
            if open_ids:
                return False
            for item in message.get("tool_calls") or []:
                open_ids.add(str(item["id"]))
        elif message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id not in open_ids:
                return False
            open_ids.discard(call_id)
    return not open_ids


def test_compact_keeps_initial_user_and_one_summary():
    session = _session()
    session.add_system("sys")
    session.add_user("MIN_L3_UNIQUE")
    for index in range(4):
        _add_round(session, f"c{index}", "payload-" + ("x" * 400))
    result = maybe_compact(session)
    assert result.did_compact is True
    assert result.folded_rounds >= 1
    users = [msg for msg in session.messages if msg.role == "user"]
    assert users[0].content == "MIN_L3_UNIQUE"
    assert users[0].is_compressed is False
    compressed = [msg for msg in session.messages if msg.is_compressed]
    assert len(compressed) == 1
    assert "不是新证据" in compressed[0].content
    maybe_compact(session)
    assert len([msg for msg in session.messages if msg.is_compressed]) == 1


def test_compact_preserves_evidence_index():
    session = _session()
    session.add_user("review")
    session.index_success(
        tool_call_id="c0",
        name="read_file",
        arguments={"path": "src/a.py", "start_line": 1, "max_lines": 2},
        location="tool:read_file:c0",
    )
    for index in range(4):
        _add_round(session, f"c{index}", "y" * 400)
    maybe_compact(session)
    assert "c0" in session.evidence_index
    assert session.evidence_index["c0"].tool_call_id == "c0"
    assert "read_file:src/a.py:L1-2" in session.checked


def test_compact_native_pairing_and_keep_rounds():
    session = _session()
    session.budget.compact_keep_rounds = 1
    session.add_user("review")
    last_id = "c3"
    for index in range(4):
        _add_round(session, f"c{index}", "z" * 400)
    maybe_compact(session)
    assert _pairing_ok(session)
    tail_ids = [msg.tool_call_id for msg in session.messages if msg.role == "tool"]
    assert last_id in tail_ids
    assert "c0" not in tail_ids


def test_compact_stubs_long_observation():
    session = _session()
    session.budget.max_session_chars = 4000
    session.budget.compact_threshold_ratio = 1.0
    session.budget.compact_keep_rounds = 2
    session.add_user("review")
    _add_round(session, "c0", "W" * 3000)
    result = maybe_compact(session)
    assert result.stubbed >= 1
    tool = next(msg for msg in session.messages if msg.role == "tool")
    assert "compacted_payload" in tool.content
    assert len(tool.content) < 3000
    assert _pairing_ok(session)


def test_compact_skipped_while_waiting_tool():
    session = _session()
    session.add_user("review")
    session.transition(ReviewTaskStatus.RUNNING)
    for index in range(4):
        _add_round(session, f"c{index}", "w" * 400)
    session.transition(ReviewTaskStatus.WAITING_TOOL)
    before = [msg.content for msg in session.messages]
    result = maybe_compact(session)
    assert result.did_compact is False
    assert [msg.content for msg in session.messages] == before


def test_not_found_goes_to_excluded():
    session = _session()
    session.index_excluded(
        name="read_file",
        arguments={"path": "missing.py", "start_line": 1, "max_lines": 1},
        error="not_found",
    )
    assert any(item.endswith(":not_found") for item in session.excluded)
    assert any(item.startswith("read_file:missing.py") for item in session.excluded)


def test_recompact_keeps_old_facts():
    session = _session()
    session.budget.compact_keep_rounds = 1
    session.add_user("review")
    _add_round(session, "c0", "x" * 400)
    _add_round(session, "c1", "x" * 400)
    first = maybe_compact(session)
    assert first.did_compact is True
    first_summary = next(msg for msg in session.messages if msg.is_compressed)
    assert "c0" in first_summary.content
    _add_round(session, "c2", "y" * 400)
    _add_round(session, "c3", "y" * 400)
    second = maybe_compact(session)
    assert second.did_compact is True
    summaries = [msg for msg in session.messages if msg.is_compressed]
    assert len(summaries) == 1
    text = summaries[0].content
    assert "c0" in text
    assert "c2" in text


def test_compact_keeps_json_repair_instruction():
    session = _session()
    session.budget.compact_keep_rounds = 0
    session.add_user("review")
    for index in range(3):
        _add_round(session, f"c{index}", "z" * 400)
    session.add_assistant("not json")
    session.add_user(
        f"你上次的输出{JSON_REPAIR_HINT}。错误：bad\n请只输出一个修正后的 JSON 对象，不要附加说明。"
    )
    session.json_repair_pending = True
    result = maybe_compact(session)
    assert result.did_compact is True
    repair_users = [
        msg
        for msg in session.messages
        if msg.role == "user" and not msg.is_compressed and JSON_REPAIR_HINT in msg.content
    ]
    assert len(repair_users) == 1
    assert session.messages[-1] is repair_users[0]


@pytest.mark.asyncio
async def test_loop_json_repair_survives_compact(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))

    class RecordingLLM(FakeLLMProvider):
        def __init__(self) -> None:
            super().__init__(
                tool_script=[
                    ModelResponse(action=None, data={"parse_error": "bad json"}),
                    _resp("finish_review", {"reason": "done"}, "tc-fin"),
                ]
            )
            self.transcripts: list[str] = []

        async def tool_loop(self, session, tools, *, budget, protocol="native"):  # type: ignore[no-untyped-def]
            self.transcripts.append("\n".join(item.content for item in session.messages))
            return await super().tool_loop(session, tools, budget=budget, protocol=protocol)

    llm = RecordingLLM()
    session = _session()
    session.budget.max_session_chars = 8000
    session.budget.compact_threshold_ratio = 0.5
    session.budget.compact_keep_rounds = 0
    session.add_user("review " + ("U" * 4500))
    budget = GlobalBudget()
    await run_agent_loop(
        session,
        llm=llm,
        registry=builtin_registry(),
        workspace=ToolWorkspace(
            repo_root=None,
            snapshot=MemoryToolSnapshot({"src/a.py": "x\n"}, head_sha="head"),
            task_id=session.task_id,
            run_id="run-a",
        ),
        global_budget=budget,
        coordinator=FinalizeBudgetCoordinator(budget),
        store=store,
        protocol="action_json",
        save_tool_trace=False,
        logger=None,
        model_sem=asyncio.Semaphore(1),
        max_output_tokens=64,
    )
    assert len(llm.transcripts) >= 2
    assert JSON_REPAIR_HINT in llm.transcripts[1]
    assert session.status is ReviewTaskStatus.COMPLETED


def test_compact_cannot_fold_huge_initial():
    session = _session()
    session.budget.max_session_chars = 500
    session.add_user("L" * 2000)
    result = maybe_compact(session)
    assert result.did_compact is False
    assert result.overflow is True
    assert session.messages[0].content.startswith("L")


@pytest.mark.asyncio
async def test_loop_compact_does_not_count_round(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    blob = "\n".join(f"line-{i:04d} " + ("body" * 20) for i in range(80))
    workspace = ToolWorkspace(
        repo_root=None,
        snapshot=MemoryToolSnapshot({"src/a.py": blob + "\n"}, head_sha="head"),
        task_id="run-a:src/a.py:agent",
        run_id="run-a",
    )
    script = [
        _resp("read_file", {"path": "src/a.py", "start_line": 1, "max_lines": 40}, "tc-0"),
        _resp("read_file", {"path": "src/a.py", "start_line": 41, "max_lines": 40}, "tc-1"),
        _resp(
            "submit_finding",
            {
                "title": "x",
                "severity": "low",
                "confidence": 0.9,
                "category": "correctness",
                "claimed_path": "src/a.py",
                "claimed_start_line": 1,
                "evidence_tool_call_ids": ["tc-0"],
            },
            "tc-sub",
        ),
        _resp("finish_review", {"reason": "done"}, "tc-fin"),
    ]
    llm = FakeLLMProvider(tool_script=script)
    session = _session()
    session.budget.max_session_chars = 80_000
    session.budget.compact_threshold_ratio = 0.08
    session.budget.compact_keep_rounds = 1
    session.add_user("review src/a.py")
    budget = GlobalBudget()
    await run_agent_loop(
        session,
        llm=llm,
        registry=builtin_registry(),
        workspace=workspace,
        global_budget=budget,
        coordinator=FinalizeBudgetCoordinator(budget),
        store=store,
        protocol="native",
        save_tool_trace=True,
        logger=None,
        model_sem=asyncio.Semaphore(1),
        max_output_tokens=64,
    )
    assert len(llm.calls) == 4
    assert any(msg.is_compressed for msg in session.messages)
    assert "tc-0" in session.evidence_index
    assert session.status is ReviewTaskStatus.COMPLETED
    assert session.candidates[0].source_kind is FindingSourceKind.TOOL_AGENT
    assert session.candidates[0].evidence[0].tool_call_id == "tc-0"
    assert any(key.startswith("read_file:src/a.py") for key in session.checked)


@pytest.mark.asyncio
async def test_loop_overflow_after_compact(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    llm = FakeLLMProvider(tool_script=[_resp("finish_review", {"reason": "x"}, "fin")])
    session = _session()
    session.budget.max_session_chars = 400
    session.add_user("Q" * 5000)
    budget = GlobalBudget()
    await run_agent_loop(
        session,
        llm=llm,
        registry=builtin_registry(),
        workspace=ToolWorkspace(
            repo_root=None,
            snapshot=MemoryToolSnapshot({"src/a.py": "x\n"}, head_sha="head"),
            task_id=session.task_id,
            run_id="run-a",
        ),
        global_budget=budget,
        coordinator=FinalizeBudgetCoordinator(budget),
        store=store,
        protocol="native",
        save_tool_trace=False,
        logger=None,
        model_sem=asyncio.Semaphore(1),
        max_output_tokens=64,
    )
    assert llm.calls == []
    assert session.stop_reason == "context_overflow"
    assert session.status is ReviewTaskStatus.PARTIAL


@pytest.mark.asyncio
async def test_default_review_still_has_no_tools(tmp_path: Path):
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/a.py": "a=1\n"})
    fake.add_snapshot("feat", {"src/a.py": "a=2\n"})
    fake.add_pr(1, base="main", head="feat")
    store = SqliteStorage(tmp_path / "d.db")
    service = ReviewService(fake, FakeLLMProvider(default_findings=[]), store)
    await service.review("1")
    n = store._query("SELECT COUNT(*) AS n FROM tool_calls")[0]["n"]  # noqa: SLF001
    assert n == 0
    assert service.settings.agent.enabled is False
def test_messages_to_chat_preserves_thinking_content_for_tool_followup():
    from reposage.domain.models import AgentMessage, AgentToolRequest

    messages = [
        AgentMessage(
            role="assistant",
            content="",
            reasoning_content="provider-required-state",
            tool_calls=[AgentToolRequest(id="tc-1", name="read_file", arguments={"path": "a.py"})],
        )
    ]
    chat = messages_to_chat(messages, protocol="native")
    assert chat[0]["reasoning_content"] == "provider-required-state"
