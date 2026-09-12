"""V3-B Agent loop 对照（24 §10.2 / T7）。脚本化 Fake，无真实模型。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from reposage.config.settings import Settings
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
from reposage.providers.git.fake import FakeGitProvider
from reposage.providers.llm.fake import FakeLLMProvider
from reposage.providers.llm.openai_compat import _action_json_response
from reposage.review.agent.budget import FinalizeBudgetCoordinator
from reposage.review.agent.loop import run_agent_loop
from reposage.review.agent.session import AgentSession
from reposage.review.service import ReviewService
from reposage.storage.sqlite import SqliteStorage
from reposage.tools import builtin_registry, invoke
from reposage.tools.read_file import ReadFileArgs
from reposage.tools.registry import HandlerOutput, ToolRegistry
from reposage.tools.snapshot import MemoryToolSnapshot, ToolWorkspace

_DEFAULT_DATASET = "reposage/evals/datasets/v3b_loop.yaml"
_DEFAULT_JSON = "docs/evidence/v3-b-compare.json"
_DEFAULT_MD = "docs/evidence/v3-b-compare.md"


class LoopEvalCase(BaseModel):
    id: str
    kind: str


class LoopEvalDataset(BaseModel):
    name: str
    cases: list[LoopEvalCase] = Field(default_factory=list)

    @classmethod
    def load_yaml(cls, path: Path) -> LoopEvalDataset:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)


def _resp(name: str, args: dict[str, Any], call_id: str, action: str | None = None) -> ModelResponse:
    mapped = action or (name if name in {"submit_finding", "finish_review"} else "tool_call")
    req = AgentToolRequest(id=call_id, name=name, arguments=args)
    return ModelResponse(action=mapped, data={"name": name, "arguments": args}, tool_requests=[req])


def _ws(task_id: str, run_id: str) -> ToolWorkspace:
    return ToolWorkspace(
        repo_root=None,
        snapshot=MemoryToolSnapshot({"src/a.py": "x = 1\n"}, head_sha="head"),
        task_id=task_id,
        run_id=run_id,
    )


def _run_script(
    script: list[ModelResponse],
    *,
    budget_kwargs: dict[str, Any] | None = None,
    protocol: str = "native",
    llm: FakeLLMProvider | None = None,
    global_budget: GlobalBudget | None = None,
    save_tool_trace: bool = False,
) -> tuple[AgentSession, FakeLLMProvider]:
    run_id = "eval-loop"
    task = ReviewTask(
        task_id=f"{run_id}:src/a.py:agent",
        run_id=run_id,
        kind=ReviewTaskKind.AGENT_TASK,
        target="src/a.py",
    )
    session = AgentSession(task, file_path="src/a.py", budget=AgentBudget(**(budget_kwargs or {})))
    session.add_user("review")
    store = SqliteStorage(":memory:")
    asyncio.run(store.record_run(ReviewRun(run_id=run_id)))
    provider = llm or FakeLLMProvider(tool_script=script)
    budget = global_budget or GlobalBudget()
    asyncio.run(
        run_agent_loop(
            session,
            llm=provider,
            registry=builtin_registry(),
            workspace=_ws(task.task_id, run_id),
            global_budget=budget,
            coordinator=FinalizeBudgetCoordinator(budget),
            store=store,
            protocol=protocol,
            save_tool_trace=save_tool_trace,
            logger=None,
            model_sem=asyncio.Semaphore(1),
            max_output_tokens=64,
        )
    )
    return session, provider


def _eval_unique_ids() -> tuple[bool, str]:
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
        async def tool_loop(self, session, tools, *, budget, protocol="native"):  # type: ignore[no-untyped-def]
            self.calls.append({"kind": "tool_loop"})
            idx = min(self._tool_i, len(texts) - 1)
            self._tool_i += 1
            return _action_json_response(texts[idx], usage)

    session, _llm = _run_script([], protocol="action_json", llm=ActionJsonLLM(), save_tool_trace=True)
    ids = list(session.evidence_refs)
    ok = len(ids) == 2 and ids[0] != ids[1] and all(item.startswith("aj-") for item in ids)
    return ok, session.status.value


async def _eval_settle_idempotent() -> tuple[bool, str]:
    budget = GlobalBudget()
    reservation = await budget.reserve(input_tokens=10, max_output_tokens=5, est_cost_usd=0.1)
    if reservation is None:
        return False, "no_reservation"
    await budget.settle(reservation, actual_input=10, actual_output=5, actual_cost=0.1)
    used = budget.tokens_used
    await budget.settle(reservation, actual_input=10, actual_output=5, actual_cost=0.1)
    ok = budget.tokens_used == used == 15
    return ok, "idempotent" if ok else "double_counted"


async def _eval_waiting_tool_cancel() -> tuple[bool, str]:
    store = SqliteStorage(":memory:")
    await store.record_run(ReviewRun(run_id="eval-loop"))
    entered = asyncio.Event()

    async def slow_handle(_workspace: ToolWorkspace, _args: object) -> HandlerOutput:
        entered.set()
        await asyncio.Event().wait()
        return HandlerOutput(payload={"ok": True})

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(name="read_file", description="slow", timeout_s=120),
        ReadFileArgs,
        slow_handle,
    )
    task = ReviewTask(
        task_id="eval-loop:src/a.py:agent",
        run_id="eval-loop",
        kind=ReviewTaskKind.AGENT_TASK,
        target="src/a.py",
    )
    session = AgentSession(task, file_path="src/a.py", budget=AgentBudget())
    session.add_user("review")
    budget = GlobalBudget()
    fut = asyncio.create_task(
        run_agent_loop(
            session,
            llm=FakeLLMProvider(
                tool_script=[_resp("read_file", {"path": "src/a.py", "start_line": 1, "max_lines": 2}, "tc-slow")]
            ),
            registry=registry,
            workspace=_ws(task.task_id, "eval-loop"),
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
    waiting = bool(rows) and rows[0]["status"] == "waiting_tool"
    fut.cancel()
    cancelled = False
    try:
        await fut
    except asyncio.CancelledError:
        cancelled = True
    rows = store._query("SELECT status FROM tasks WHERE task_id=?", (session.task_id,))  # noqa: SLF001
    ok = waiting and cancelled and bool(rows) and rows[0]["status"] == "cancelled"
    return ok, rows[0]["status"] if rows else "missing"


async def _eval_shared_pool_concurrent() -> tuple[bool, str]:
    budget = GlobalBudget(max_total_tokens=1000, max_cost_usd=10.0, reserved_finalize_ratio=0.10)
    coord = FinalizeBudgetCoordinator(budget)

    async def grab() -> object:
        return await coord.reserve(mode="grace", input_tokens=80, max_output_tokens=0, est_cost_usd=0.5)

    first, second = await asyncio.gather(grab(), grab())
    winners = [item for item in (first, second) if item is not None]
    ok = len(winners) == 1
    return ok, "one_winner" if ok else f"winners={len(winners)}"


def _eval_case(case: LoopEvalCase) -> dict[str, Any]:
    ok = False
    status = ""
    detail = ""
    if case.id == "happy-read-submit-finish":
        session, _llm = _run_script(
            [
                _resp("read_file", {"path": "src/a.py", "start_line": 1, "max_lines": 10}, "tc-read"),
                _resp(
                    "submit_finding",
                    {
                        "title": "x",
                        "severity": "high",
                        "confidence": 0.9,
                        "category": "correctness",
                        "claimed_path": "src/a.py",
                        "claimed_start_line": 1,
                        "evidence_tool_call_ids": ["tc-read"],
                    },
                    "tc-sub",
                ),
                _resp("finish_review", {"reason": "done"}, "tc-fin"),
            ]
        )
        ok = (
            session.status is ReviewTaskStatus.COMPLETED
            and bool(session.candidates)
            and session.candidates[0].source_kind is FindingSourceKind.TOOL_AGENT
        )
        status = session.status.value
    elif case.id == "unknown-tool-attempt":
        session, _llm = _run_script(
            [
                _resp("missing", {}, "tc-x"),
                _resp("finish_review", {"reason": "done"}, "tc-fin"),
            ]
        )
        ok = session.tool_attempts == 1 and session.successful_tools == 0
        status = session.status.value
    elif case.id == "repeat-loop":
        args = {"path": "src/a.py", "start_line": 1, "max_lines": 2}
        session, _llm = _run_script(
            [_resp("read_file", args, f"tc-{i}") for i in range(5)],
            budget_kwargs={"repeat_threshold": 2},
        )
        ok = session.status is ReviewTaskStatus.PARTIAL and session.stop_reason == "repeat_loop"
        status = session.status.value
    elif case.id == "idle-no-progress":
        session, _llm = _run_script([ModelResponse(action=None, data={}) for _ in range(4)])
        ok = session.status is ReviewTaskStatus.PARTIAL and session.stop_reason == "no_progress"
        status = session.status.value
    elif case.id == "grace-no-new-tools":
        session, _llm = _run_script(
            [
                _resp("read_file", {"path": "src/a.py"}, "tc-late"),
                _resp("finish_review", {"reason": "stop"}, "tc-fin"),
            ],
            budget_kwargs={"max_tool_calls": 0, "grace_rounds": 3},
        )
        ok = session.mode == "grace" and session.successful_tools == 0
        status = session.status.value
    elif case.id == "stub-invoke-not-bound":
        call, result = asyncio.run(
            invoke(
                builtin_registry(),
                _ws("t", "r"),
                "submit_finding",
                {"title": "x", "severity": "low", "confidence": 0.5, "category": "correctness"},
            )
        )
        ok = call.status is ToolCallStatus.ERROR and result.error == "not_bound"
        status = call.status.value
    elif case.id == "default-review-isolation":
        fake = FakeGitProvider()
        fake.add_snapshot("main", {"src/a.py": "def f():\n    return 1\n"})
        fake.add_snapshot("feat", {"src/a.py": "def f():\n    return 2\n"})
        fake.add_pr(1, base="main", head="feat")
        store = SqliteStorage(":memory:")
        service = ReviewService(fake, FakeLLMProvider(default_findings=[]), store)
        asyncio.run(service.review("1"))
        n = store._query("SELECT COUNT(*) AS n FROM tool_calls")[0]["n"]  # noqa: SLF001
        ok = n == 0 and service.settings.agent.enabled is False
        status = "ok" if ok else "error"
    elif case.id == "agentic-disabled-reject":
        settings = Settings.model_validate({"review": {"strategy": "agentic"}})
        try:
            ReviewService(FakeGitProvider(), FakeLLMProvider(), SqliteStorage(":memory:"), settings=settings)
            ok = False
            detail = "did_not_raise"
        except ValueError as exc:
            ok = "agent.enabled" in str(exc)
            detail = str(exc)
        status = "rejected" if ok else "error"
    elif case.id == "early-grace-cap":
        args = {"path": "src/a.py", "start_line": 1, "max_lines": 2}
        session, llm = _run_script(
            [_resp("read_file", args, f"tc-{i}") for i in range(6)],
            budget_kwargs={"max_rounds": 8, "grace_rounds": 2, "max_tool_calls": 1},
        )
        ok = (
            len(llm.calls) == 3
            and session.mode == "grace"
            and session.grace_rounds_used == 2
            and session.stop_reason == "budget"
        )
        status = session.status.value
    elif case.id == "json-repair-counts-round":
        session, llm = _run_script(
            [
                ModelResponse(action=None, data={"parse_error": "bad json"}),
                _resp("finish_review", {"reason": "done"}, "tc-fin"),
            ],
            protocol="action_json",
        )
        ok = session.rounds_used == 2 and session.json_repair_used and session.status is ReviewTaskStatus.COMPLETED
        status = session.status.value
    elif case.id == "unique-same-name-tool-ids":
        ok, status = _eval_unique_ids()
    elif case.id == "settle-idempotent":
        ok, status = asyncio.run(_eval_settle_idempotent())
    elif case.id == "waiting-tool-cancel":
        ok, status = asyncio.run(_eval_waiting_tool_cancel())
    elif case.id == "shared-pool-concurrent":
        ok, status = asyncio.run(_eval_shared_pool_concurrent())
    else:
        detail = "unknown_case"
    return {"id": case.id, "kind": case.kind, "ok": ok, "status": status, "detail": detail}


def run_compare(dataset_path: str | Path = _DEFAULT_DATASET) -> dict[str, Any]:
    dataset = LoopEvalDataset.load_yaml(Path(dataset_path))
    rows = [_eval_case(case) for case in dataset.cases]
    passed = sum(1 for r in rows if r["ok"])
    return {
        "dataset": dataset.name,
        "samples": len(rows),
        "real_api": False,
        "loop": {
            "passed": passed,
            "total": len(rows),
            "cases": rows,
            "note": "脚本化 Fake 轨迹；无真实模型。agent.enabled 默认 false。experimental。",
        },
        "all_passed": passed == len(rows),
    }


def render_markdown(report: dict[str, Any]) -> str:
    box = report["loop"]
    lines = [
        "# V3-B Agent Loop 对照（脚本化 Fake，非真实 API）",
        "",
        "> 由 `python -m reposage.evals.v3b_compare` 生成；原始 JSON：`docs/evidence/v3-b-compare.json`。",
        f"> dataset=`{report['dataset']}` samples={report['samples']} real_api={report['real_api']}",
        "> Agent 为 experimental；OQ-11 live 后置。脱敏：不含 API Key / 源码原文。",
        "",
        "## 轨迹 / 控制工具 / 隔离 / 预算",
        "",
        f"passed={box['passed']}/{box['total']}",
        "",
        "| id | kind | ok | status |",
        "|----|------|----|--------|",
    ]
    for row in box["cases"]:
        lines.append(f"| {row['id']} | {row['kind']} | {row['ok']} | {row['status']} |")
    lines.extend(["", box["note"], "", f"全部通过：{report['all_passed']}", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=_DEFAULT_DATASET)
    parser.add_argument("--json-out", default=_DEFAULT_JSON)
    parser.add_argument("--md-out", default=_DEFAULT_MD)
    args = parser.parse_args(argv)
    report = run_compare(args.dataset)
    Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(args.md_out).write_text(render_markdown(report), encoding="utf-8")
    print(f"passed={report['loop']['passed']}/{report['loop']['total']} all_passed={report['all_passed']}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
