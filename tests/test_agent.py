"""V3-B Agent loop / 状态机 / 预算 / Service 接线。"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from reposage.config.settings import AgentConfig, Settings
from reposage.domain.enums import (
    FindingSourceKind,
    ReviewTaskKind,
    ReviewTaskStatus,
    ToolCallStatus,
)
from reposage.domain.models import (
    AgentBudget,
    AgentToolRequest,
    GlobalBudget,
    ModelUsage,
    ToolDefinition,
)
from reposage.domain.protocols import ModelResponse
from reposage.domain.run import ReviewRun, ReviewTask
from reposage.observability.logging import StructuredLogger
from reposage.providers.git.fake import FakeGitProvider
from reposage.providers.llm.fake import FakeLLMProvider
from reposage.providers.llm.openai_compat import LLMRequestError, _action_json_response
from reposage.review.agent.budget import FinalizeBudgetCoordinator
from reposage.review.agent.loop import run_agent_loop
from reposage.review.agent.session import AgentSession, AgentStateError, allowed_transition
from reposage.review.service import ReviewService
from reposage.storage.sqlite import SqliteStorage
from reposage.tools import builtin_registry, invoke
from reposage.tools.read_file import ReadFileArgs
from reposage.tools.registry import HandlerOutput, ToolRegistry
from reposage.tools.snapshot import MemoryToolSnapshot, ToolWorkspace


def _task(run_id: str = "run-a", path: str = "src/a.py") -> ReviewTask:
    return ReviewTask(
        task_id=f"{run_id}:{path}:agent",
        run_id=run_id,
        kind=ReviewTaskKind.AGENT_TASK,
        target=path,
    )


def _session(**kwargs: object) -> AgentSession:
    budget = AgentBudget(max_rounds=8, max_tool_calls=12, max_tool_attempts=36, grace_rounds=2)
    return AgentSession(_task(), file_path="src/a.py", budget=budget)


def _ws() -> ToolWorkspace:
    return ToolWorkspace(
        repo_root=None,
        snapshot=MemoryToolSnapshot({"src/a.py": "value = 1\n"}, head_sha="head"),
        task_id="run-a:src/a.py:agent",
        run_id="run-a",
    )


def _resp(action: str, name: str, args: dict[str, object], *, call_id: str) -> ModelResponse:
    req = AgentToolRequest(id=call_id, name=name, arguments=args)
    mapped = name if action in {"submit_finding", "finish_review"} else action
    return ModelResponse(
        text="",
        action=mapped,
        data={"name": name, "arguments": args},
        tool_requests=[req],
    )


def test_illegal_waiting_tool_to_completed():
    assert not allowed_transition(ReviewTaskStatus.WAITING_TOOL, ReviewTaskStatus.COMPLETED)
    session = _session()
    session.transition(ReviewTaskStatus.RUNNING)
    session.transition(ReviewTaskStatus.WAITING_TOOL)
    with pytest.raises(AgentStateError):
        session.transition(ReviewTaskStatus.COMPLETED)


def test_agent_config_attempts_default():
    cfg = AgentConfig()
    assert cfg.max_tool_attempts is None
    assert cfg.effective_max_tool_attempts() == 36
    assert cfg.compact_threshold_ratio == 0.60
    assert cfg.compact_keep_rounds == 2
    assert cfg.max_session_chars == 200_000
    with pytest.raises(ValidationError):
        AgentConfig(max_tool_attempts=0)
    with pytest.raises(ValidationError):
        AgentConfig(max_session_chars=100)


@pytest.mark.asyncio
async def test_finalize_pool_shared_not_copied_per_file():
    budget = GlobalBudget(max_total_tokens=1000, max_cost_usd=10.0, reserved_finalize_ratio=0.10)
    coord = FinalizeBudgetCoordinator(budget)
    assert coord.pool_tokens == 100
    first = await coord.reserve(mode="exploration", input_tokens=901, max_output_tokens=0, est_cost_usd=1.0)
    assert first is None
    ok = await coord.reserve(mode="exploration", input_tokens=800, max_output_tokens=0, est_cost_usd=1.0)
    assert ok is not None
    await coord.settle(ok, mode="exploration", actual_input=800, actual_output=0, actual_cost=1.0)
    grace = await coord.reserve(mode="grace", input_tokens=100, max_output_tokens=0, est_cost_usd=1.0)
    assert grace is not None
    overflow = await coord.reserve(mode="grace", input_tokens=1, max_output_tokens=0, est_cost_usd=0.1)
    assert overflow is None


@pytest.mark.asyncio
async def test_happy_path_read_submit_finish(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    run = ReviewRun(run_id="run-a")
    await store.record_run(run)
    script = [
        _resp("tool_call", "read_file", {"path": "src/a.py", "start_line": 1, "max_lines": 10}, call_id="tc-read"),
        _resp(
            "submit_finding",
            "submit_finding",
            {
                "title": "bug",
                "severity": "high",
                "confidence": 0.9,
                "category": "correctness",
                "claimed_path": "src/a.py",
                "claimed_start_line": 1,
                "evidence_tool_call_ids": ["tc-read"],
            },
            call_id="tc-sub",
        ),
        _resp("finish_review", "finish_review", {"reason": "done"}, call_id="tc-fin"),
    ]
    llm = FakeLLMProvider(tool_script=script)
    session = _session()
    session.add_system("sys")
    session.add_user("review src/a.py")
    budget = GlobalBudget(max_total_tokens=80000, max_cost_usd=2.0)
    await run_agent_loop(
        session,
        llm=llm,
        registry=builtin_registry(),
        workspace=_ws(),
        global_budget=budget,
        coordinator=FinalizeBudgetCoordinator(budget),
        store=store,
        protocol="native",
        save_tool_trace=True,
        logger=None,
        model_sem=asyncio.Semaphore(1),
        max_output_tokens=128,
    )
    assert session.status is ReviewTaskStatus.COMPLETED
    assert len(session.candidates) == 1
    assert session.candidates[0].source_kind is FindingSourceKind.TOOL_AGENT
    rows = store._query("SELECT status FROM tasks WHERE task_id=?", (session.task_id,))  # noqa: SLF001
    assert rows[0]["status"] == "completed"
    tools = store._query("SELECT name FROM tool_calls WHERE task_id=?", (session.task_id,))  # noqa: SLF001
    assert any(r["name"] == "read_file" for r in tools)


@pytest.mark.asyncio
async def test_unknown_tool_counts_attempt_not_success(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    script = [
        _resp("tool_call", "not_a_tool", {}, call_id="tc-x"),
        _resp("finish_review", "finish_review", {"reason": "done"}, call_id="tc-fin"),
    ]
    session = _session()
    session.add_user("go")
    budget = GlobalBudget()
    await run_agent_loop(
        session,
        llm=FakeLLMProvider(tool_script=script),
        registry=builtin_registry(),
        workspace=_ws(),
        global_budget=budget,
        coordinator=FinalizeBudgetCoordinator(budget),
        store=store,
        protocol="native",
        save_tool_trace=True,
        logger=None,
        model_sem=asyncio.Semaphore(1),
        max_output_tokens=64,
    )
    assert session.tool_attempts == 1
    assert session.successful_tools == 0
    assert session.status is ReviewTaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_repeat_loop_partial(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    args = {"path": "src/a.py", "start_line": 1, "max_lines": 2}
    script = [
        _resp("tool_call", "read_file", args, call_id=f"tc-{i}") for i in range(5)
    ]
    session = _session()
    session.budget.repeat_threshold = 2
    session.add_user("go")
    budget = GlobalBudget()
    await run_agent_loop(
        session,
        llm=FakeLLMProvider(tool_script=script),
        registry=builtin_registry(),
        workspace=_ws(),
        global_budget=budget,
        coordinator=FinalizeBudgetCoordinator(budget),
        store=store,
        protocol="native",
        save_tool_trace=True,
        logger=None,
        model_sem=asyncio.Semaphore(1),
        max_output_tokens=64,
    )
    assert session.status is ReviewTaskStatus.PARTIAL
    assert session.stop_reason == "repeat_loop"


@pytest.mark.asyncio
async def test_idle_no_progress(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    script = [ModelResponse(action=None, data={}) for _ in range(4)]
    session = _session()
    session.add_user("go")
    budget = GlobalBudget()
    await run_agent_loop(
        session,
        llm=FakeLLMProvider(tool_script=script),
        registry=builtin_registry(),
        workspace=_ws(),
        global_budget=budget,
        coordinator=FinalizeBudgetCoordinator(budget),
        store=store,
        protocol="native",
        save_tool_trace=False,
        logger=None,
        model_sem=asyncio.Semaphore(1),
        max_output_tokens=64,
    )
    assert session.status is ReviewTaskStatus.PARTIAL
    assert session.stop_reason == "no_progress"


@pytest.mark.asyncio
async def test_grace_rejects_readonly_and_submit_without_evidence(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    session = _session()
    session.budget.max_tool_calls = 0
    session.budget.grace_rounds = 3
    session.add_user("go")
    script = [
        _resp("tool_call", "read_file", {"path": "src/a.py"}, call_id="tc-late"),
        _resp(
            "submit_finding",
            "submit_finding",
            {
                "title": "x",
                "severity": "low",
                "confidence": 0.9,
                "category": "correctness",
            },
            call_id="tc-sub",
        ),
        _resp("finish_review", "finish_review", {"reason": "stop"}, call_id="tc-fin"),
    ]
    budget = GlobalBudget()
    await run_agent_loop(
        session,
        llm=FakeLLMProvider(tool_script=script),
        registry=builtin_registry(),
        workspace=_ws(),
        global_budget=budget,
        coordinator=FinalizeBudgetCoordinator(budget),
        store=store,
        protocol="native",
        save_tool_trace=False,
        logger=None,
        model_sem=asyncio.Semaphore(1),
        max_output_tokens=64,
    )
    assert session.mode == "grace"
    assert session.successful_tools == 0
    assert session.candidates == []
    assert session.status is ReviewTaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_cancel_waiting_tool_persists_and_raises(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    entered = asyncio.Event()

    async def slow_handle(_workspace: object, _args: object) -> HandlerOutput:
        entered.set()
        await asyncio.Event().wait()
        return HandlerOutput(payload={"ok": True})

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(name="read_file", description="slow", timeout_s=120),
        ReadFileArgs,
        slow_handle,
    )
    session = _session()
    session.add_user("go")
    budget = GlobalBudget()
    task = asyncio.create_task(
        run_agent_loop(
            session,
            llm=FakeLLMProvider(
                tool_script=[
                    _resp(
                        "tool_call",
                        "read_file",
                        {"path": "src/a.py", "start_line": 1, "max_lines": 2},
                        call_id="tc-slow",
                    )
                ]
            ),
            registry=registry,
            workspace=_ws(),
            global_budget=budget,
            coordinator=FinalizeBudgetCoordinator(budget),
            store=store,
            protocol="native",
            save_tool_trace=False,
            logger=None,
            model_sem=asyncio.Semaphore(1),
            max_output_tokens=64,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    rows = store._query("SELECT status FROM tasks WHERE task_id=?", (session.task_id,))  # noqa: SLF001
    assert rows[0]["status"] == "waiting_tool"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = store._query("SELECT status FROM tasks WHERE task_id=?", (session.task_id,))  # noqa: SLF001
    assert rows[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_native_multiple_tool_calls_pair_observations(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    first = AgentToolRequest(id="c1", name="read_file", arguments={"path": "src/a.py", "start_line": 1, "max_lines": 2})
    second = AgentToolRequest(id="c2", name="read_file", arguments={"path": "src/a.py", "start_line": 2, "max_lines": 2})
    script = [
        ModelResponse(
            action="tool_call",
            data={"name": "read_file", "arguments": first.arguments},
            tool_requests=[first, second],
        ),
        _resp("finish_review", "finish_review", {"reason": "done"}, call_id="fin"),
    ]
    session = _session()
    session.add_user("go")
    budget = GlobalBudget()
    await run_agent_loop(
        session,
        llm=FakeLLMProvider(tool_script=script),
        registry=builtin_registry(),
        workspace=_ws(),
        global_budget=budget,
        coordinator=FinalizeBudgetCoordinator(budget),
        store=store,
        protocol="native",
        save_tool_trace=False,
        logger=None,
        model_sem=asyncio.Semaphore(1),
        max_output_tokens=64,
    )
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    ids = {m.tool_call_id for m in tool_msgs}
    assert "c1" in ids and "c2" in ids
    skipped = next(m for m in tool_msgs if m.tool_call_id == "c2")
    assert "not_executed_multiple_calls" in skipped.content
    assert session.tool_attempts == 2


@pytest.mark.asyncio
async def test_direct_invoke_submit_still_not_bound():
    call, result = await invoke(builtin_registry(), _ws(), "submit_finding", {
        "title": "x",
        "severity": "low",
        "confidence": 0.5,
        "category": "correctness",
    })
    assert call.status is ToolCallStatus.ERROR
    assert result.error == "not_bound"


@pytest.mark.asyncio
async def test_service_agentic_requires_enabled():
    settings = Settings.model_validate({"review": {"strategy": "agentic"}})
    with pytest.raises(ValueError, match="agent.enabled"):
        ReviewService(FakeGitProvider(), FakeLLMProvider(), SqliteStorage(":memory:"), settings=settings)


@pytest.mark.asyncio
async def test_service_agentic_end_to_end(tmp_path: Path):
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/a.py": "def f():\n    return 1\n"})
    fake.add_snapshot("feat", {"src/a.py": "def f():\n    return 2\n"})
    fake.add_pr(1, base="main", head="feat")
    script = [
        _resp("tool_call", "read_file", {"path": "src/a.py", "start_line": 1, "max_lines": 20}, call_id="tc-read"),
        _resp(
            "submit_finding",
            "submit_finding",
            {
                "title": "return changed",
                "severity": "medium",
                "confidence": 0.9,
                "category": "correctness",
                "claimed_path": "src/a.py",
                "claimed_start_line": 2,
                "evidence_tool_call_ids": ["tc-read"],
            },
            call_id="tc-sub",
        ),
        _resp("finish_review", "finish_review", {"reason": "done"}, call_id="tc-fin"),
    ]
    settings = Settings.model_validate(
        {"review": {"strategy": "agentic"}, "agent": {"enabled": True, "max_rounds": 6}}
    )
    store = SqliteStorage(tmp_path / "svc.db")
    assert getattr(fake, "repo_root", None) is None
    service = ReviewService(fake, FakeLLMProvider(tool_script=script), store, settings=settings)
    run, findings = await service.review("1")
    assert run.strategy.value == "agentic"
    assert any(f.sources and f.sources[0].kind is FindingSourceKind.TOOL_AGENT for f in findings) or findings == []
    n = store._query("SELECT COUNT(*) AS n FROM tool_calls")[0]["n"]  # noqa: SLF001
    assert n >= 1


@pytest.mark.asyncio
async def test_parent_cancel_cancels_siblings(tmp_path: Path):
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"a.py": "a=1\n", "b.py": "b=1\n"})
    fake.add_snapshot("feat", {"a.py": "a=2\n", "b.py": "b=2\n"})
    fake.add_pr(1, base="main", head="feat")

    started = asyncio.Event()
    count = 0
    lock = asyncio.Lock()

    class HangLLM(FakeLLMProvider):
        async def tool_loop(self, session, tools, *, budget, protocol="native"):  # type: ignore[no-untyped-def]
            nonlocal count
            async with lock:
                count += 1
                if count >= 2:
                    started.set()
            await asyncio.sleep(30)
            return ModelResponse(action="finish_review", data={"arguments": {"reason": "x"}})

    settings = Settings.model_validate(
        {
            "review": {"strategy": "agentic"},
            "agent": {"enabled": True},
            "concurrency": {"file_tasks": 2, "model_requests": 2},
        }
    )
    store = SqliteStorage(tmp_path / "c.db")
    service = ReviewService(fake, HangLLM(), store, settings=settings)
    task = asyncio.create_task(service.review("1"))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = store._query("SELECT status FROM tasks WHERE kind=?", ("agent_task",))  # noqa: SLF001
    assert len(rows) >= 2
    assert all(row["status"] == "cancelled" for row in rows)


def _parse_error(msg: str = "bad json") -> ModelResponse:
    return ModelResponse(action=None, data={"parse_error": msg})


async def _loop(
    session: AgentSession,
    llm: FakeLLMProvider,
    store: SqliteStorage,
    *,
    protocol: str = "native",
    budget: GlobalBudget | None = None,
    coordinator: FinalizeBudgetCoordinator | None = None,
    registry: ToolRegistry | None = None,
    save_tool_trace: bool = False,
    max_output_tokens: int = 64,
    logger: StructuredLogger | None = None,
) -> GlobalBudget:
    budget = budget or GlobalBudget()
    await run_agent_loop(
        session,
        llm=llm,
        registry=registry or builtin_registry(),
        workspace=_ws(),
        global_budget=budget,
        coordinator=coordinator or FinalizeBudgetCoordinator(budget),
        store=store,
        protocol=protocol,
        save_tool_trace=save_tool_trace,
        logger=logger,
        model_sem=asyncio.Semaphore(1),
        max_output_tokens=max_output_tokens,
    )
    return budget


@pytest.mark.asyncio
async def test_early_grace_caps_provider_rounds(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    args = {"path": "src/a.py", "start_line": 1, "max_lines": 2}
    script = [_resp("tool_call", "read_file", args, call_id=f"tc-{i}") for i in range(6)]
    llm = FakeLLMProvider(tool_script=script)
    session = _session()
    session.budget.max_rounds = 8
    session.budget.grace_rounds = 2
    session.budget.max_tool_calls = 1
    session.add_user("go")
    buf = io.StringIO()
    await _loop(session, llm, store, logger=StructuredLogger(sink=buf))
    assert len(llm.calls) == 3
    assert session.mode == "grace"
    assert session.grace_rounds_used == 2
    assert session.status is ReviewTaskStatus.PARTIAL
    assert session.stop_reason == "budget"
    assert buf.getvalue().count("enter_grace") == 1


@pytest.mark.asyncio
async def test_grace_rounds_zero_sends_no_grace_request(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    llm = FakeLLMProvider(
        tool_script=[
            _resp("tool_call", "read_file", {"path": "src/a.py", "start_line": 1, "max_lines": 2}, call_id="tc-1"),
            _resp("finish_review", "finish_review", {"reason": "done"}, call_id="tc-fin"),
        ]
    )
    session = _session()
    session.budget.max_tool_calls = 0
    session.budget.grace_rounds = 0
    session.add_user("go")
    await _loop(session, llm, store)
    assert llm.calls == []
    assert session.mode == "grace"
    assert session.stop_reason == "budget"


@pytest.mark.asyncio
async def test_action_json_repair_success_counts_round(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    llm = FakeLLMProvider(
        tool_script=[
            _parse_error(),
            _resp("finish_review", "finish_review", {"reason": "done"}, call_id="tc-fin"),
        ]
    )
    session = _session()
    session.add_user("go")
    await _loop(session, llm, store, protocol="action_json")
    assert len(llm.calls) == 2
    assert session.rounds_used == 2
    assert session.json_repair_used is True
    assert session.status is ReviewTaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_action_json_repair_still_fails(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    llm = FakeLLMProvider(tool_script=[_parse_error("one"), _parse_error("two")])
    session = _session()
    session.budget.max_rounds = 2
    session.add_user("go")
    await _loop(session, llm, store, protocol="action_json")
    assert len(llm.calls) == 2
    assert session.json_repair_used is True
    assert session.idle_count >= 1
    assert any("invalid_action" in m.content for m in session.messages if m.role == "user")
    assert session.stop_reason == "budget"


@pytest.mark.asyncio
async def test_action_json_repair_skipped_when_max_rounds_spent(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    llm = FakeLLMProvider(tool_script=[_parse_error(), _resp("finish_review", "finish_review", {"reason": "x"}, call_id="x")])
    session = _session()
    session.budget.max_rounds = 1
    session.add_user("go")
    await _loop(session, llm, store, protocol="action_json")
    assert len(llm.calls) == 1
    assert session.json_repair_used is False
    assert session.stop_reason == "budget"


@pytest.mark.asyncio
async def test_action_json_repair_does_not_borrow_finalize(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    llm = FakeLLMProvider(
        tool_script=[_parse_error(), _resp("finish_review", "finish_review", {"reason": "x"}, call_id="x")],
        input_tokens=180,
        output_tokens=20,
    )
    session = _session()
    session.add_user("go")
    budget = GlobalBudget(max_total_tokens=400, reserved_finalize_ratio=0.5)
    await _loop(session, llm, store, protocol="action_json", budget=budget)
    assert len(llm.calls) == 1
    assert session.mode == "exploration"
    assert session.json_repair_used is False
    assert session.stop_reason == "budget"


@pytest.mark.asyncio
async def test_action_json_same_name_tools_get_unique_ids(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    usage = ModelUsage(model="fake", role="test", input_tokens=10, output_tokens=5, cost_usd=0.0)
    texts = [
        json.dumps(
            {
                "action": "tool_call",
                "name": "read_file",
                "args": {"path": "src/a.py", "start_line": 1, "max_lines": 1},
            }
        ),
        json.dumps(
            {
                "action": "tool_call",
                "name": "read_file",
                "args": {"path": "src/a.py", "start_line": 2, "max_lines": 1},
            }
        ),
        json.dumps({"action": "finish_review", "args": {"reason": "done"}}),
    ]

    class ActionJsonLLM(FakeLLMProvider):
        def __init__(self) -> None:
            super().__init__()
            self._texts = list(texts)

        async def tool_loop(self, session, tools, *, budget, protocol="native"):  # type: ignore[no-untyped-def]
            self.calls.append({"kind": "tool_loop", "protocol": protocol})
            text = self._texts[min(self._tool_i, len(self._texts) - 1)]
            self._tool_i += 1
            return _action_json_response(text, usage)

    llm = ActionJsonLLM()
    session = _session()
    session.add_user("go")
    await _loop(session, llm, store, protocol="action_json", save_tool_trace=True)
    ids = [cid for cid in session.evidence_refs]
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert all(item.startswith("aj-") for item in ids)
    rows = store._query("SELECT tool_call_id FROM tool_calls")  # noqa: SLF001
    persisted = {row["tool_call_id"] for row in rows}
    assert persisted == set(ids)


@pytest.mark.asyncio
async def test_settle_is_idempotent():
    budget = GlobalBudget()
    reservation = await budget.reserve(input_tokens=10, max_output_tokens=5, est_cost_usd=0.1)
    assert reservation is not None
    await budget.settle(reservation, actual_input=10, actual_output=5, actual_cost=0.1)
    used = budget.tokens_used
    cost = budget.cost_used
    await budget.settle(reservation, actual_input=10, actual_output=5, actual_cost=0.1)
    assert budget.tokens_used == used
    assert budget.cost_used == cost
    assert reservation.settled is True


@pytest.mark.asyncio
async def test_coordinator_grace_settle_is_idempotent():
    budget = GlobalBudget(max_total_tokens=1000, max_cost_usd=10.0, reserved_finalize_ratio=0.10)
    coord = FinalizeBudgetCoordinator(budget)
    reservation = await coord.reserve(mode="grace", input_tokens=80, max_output_tokens=0, est_cost_usd=0.5)
    assert reservation is not None
    await coord.settle(reservation, mode="grace", actual_input=80, actual_output=0, actual_cost=0.5)
    used = coord._grace_used_tokens  # noqa: SLF001
    await coord.settle(reservation, mode="grace", actual_input=80, actual_output=0, actual_cost=0.5)
    assert coord._grace_used_tokens == used  # noqa: SLF001
    assert id(reservation) not in coord._grace_open  # noqa: SLF001


@pytest.mark.asyncio
async def test_cancel_at_settle_boundary_does_not_double_count(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))

    class SettleThenCancel(FinalizeBudgetCoordinator):
        async def settle(self, reservation, **kwargs):  # type: ignore[no-untyped-def]
            await super().settle(reservation, **kwargs)
            raise asyncio.CancelledError

    llm = FakeLLMProvider(
        tool_script=[_resp("finish_review", "finish_review", {"reason": "done"}, call_id="fin")],
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
    )
    session = _session()
    session.add_user("go")
    budget = GlobalBudget()
    with pytest.raises(asyncio.CancelledError):
        await _loop(session, llm, store, budget=budget, coordinator=SettleThenCancel(budget))
    assert budget.tokens_used == 150
    assert budget.cost_used == 0.0


@pytest.mark.asyncio
async def test_finalize_pool_concurrent_contention():
    budget = GlobalBudget(max_total_tokens=1000, max_cost_usd=10.0, reserved_finalize_ratio=0.10)
    coord = FinalizeBudgetCoordinator(budget)

    async def grab() -> object:
        return await coord.reserve(mode="grace", input_tokens=80, max_output_tokens=0, est_cost_usd=0.5)

    first, second = await asyncio.gather(grab(), grab())
    winners = [item for item in (first, second) if item is not None]
    assert len(winners) == 1


@pytest.mark.asyncio
async def test_multi_tool_control_extra_does_not_count_attempt(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))
    first = AgentToolRequest(id="c1", name="read_file", arguments={"path": "src/a.py", "start_line": 1, "max_lines": 2})
    extra = AgentToolRequest(id="c2", name="finish_review", arguments={"reason": "done"})
    script = [
        ModelResponse(action="tool_call", data={"name": "read_file", "arguments": first.arguments}, tool_requests=[first, extra]),
        _resp("finish_review", "finish_review", {"reason": "done"}, call_id="fin"),
    ]
    session = _session()
    session.add_user("go")
    await _loop(session, FakeLLMProvider(tool_script=script), store)
    assert session.tool_attempts == 1


@pytest.mark.asyncio
async def test_model_error_is_not_labeled_timeout(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))

    class BoomLLM(FakeLLMProvider):
        async def tool_loop(self, session, tools, *, budget, protocol="native"):  # type: ignore[no-untyped-def]
            raise RuntimeError("auth failed sk-SECRETKEY1234")

    session = _session()
    session.add_user("go")
    await _loop(session, BoomLLM(), store)
    assert session.stop_reason.startswith("model_error:")
    assert "sk-SECRETKEY1234" not in session.stop_reason
    assert session.stop_reason != "model_timeout"


@pytest.mark.asyncio
async def test_timeout_error_is_model_timeout(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))

    class TimeoutLLM(FakeLLMProvider):
        async def tool_loop(self, session, tools, *, budget, protocol="native"):  # type: ignore[no-untyped-def]
            raise TimeoutError("slow")

    session = _session()
    session.add_user("go")
    await _loop(session, TimeoutLLM(), store)
    assert session.stop_reason == "model_timeout"


@pytest.mark.asyncio
async def test_protocol_error_reason(tmp_path: Path):
    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-a"))

    class ProtoLLM(FakeLLMProvider):
        async def tool_loop(self, session, tools, *, budget, protocol="native"):  # type: ignore[no-untyped-def]
            raise LLMRequestError("tools unsupported", status_code=400, error_body="tool_choice is not supported")

    session = _session()
    session.add_user("go")
    await _loop(session, ProtoLLM(), store)
    assert session.stop_reason.startswith("protocol_error:")


@pytest.mark.asyncio
async def test_concurrent_reviews_do_not_mix_workspaces(tmp_path: Path):
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/a.py": "base\n"})
    fake.add_snapshot("feat1", {"src/a.py": "feat1-AAA unique-head-one\n"})
    fake.add_snapshot("feat2", {"src/a.py": "feat2-BBB unique-head-two\n"})
    fake.add_pr(1, base="main", head="feat1")
    fake.add_pr(2, base="main", head="feat2")

    class PerSessionLLM(FakeLLMProvider):
        def __init__(self) -> None:
            super().__init__()
            self._n: dict[str, int] = {}

        async def tool_loop(self, session, tools, *, budget, protocol="native"):  # type: ignore[no-untyped-def]
            sid = session.session_id
            n = self._n.get(sid, 0)
            self._n[sid] = n + 1
            if n == 0:
                return _resp(
                    "tool_call",
                    "read_file",
                    {"path": "src/a.py", "start_line": 1, "max_lines": 20},
                    call_id=f"{sid}-read",
                )
            return _resp("finish_review", "finish_review", {"reason": "done"}, call_id=f"{sid}-fin")

    settings = Settings.model_validate({"review": {"strategy": "agentic"}, "agent": {"enabled": True}})
    store = SqliteStorage(tmp_path / "mix.db")
    service = ReviewService(fake, PerSessionLLM(), store, settings=settings)
    await asyncio.gather(service.review("1"), service.review("2"))
    blobs = [row["data"] or "" for row in store._query("SELECT data FROM tool_results")]  # noqa: SLF001
    assert any("feat1-AAA" in blob for blob in blobs)
    assert any("feat2-BBB" in blob for blob in blobs)
    assert not any("feat1-AAA" in blob and "feat2-BBB" in blob for blob in blobs)
