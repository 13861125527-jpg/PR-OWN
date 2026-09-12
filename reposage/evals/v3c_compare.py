"""V3-C 会话压缩对照（25 §9.2 / T5）。脚本化 Fake，无真实模型。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from reposage.domain.enums import ReviewTaskKind
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

_DEFAULT_DATASET = "reposage/evals/datasets/v3c_compact.yaml"
_DEFAULT_JSON = "docs/evidence/v3-c-compare.json"
_DEFAULT_MD = "docs/evidence/v3-c-compare.md"
_LOOP_BUDGET = {
    "max_rounds": 8,
    "max_session_chars": 80_000,
    "compact_threshold_ratio": 0.08,
    "compact_keep_rounds": 1,
}


class CompactEvalCase(BaseModel):
    id: str
    kind: str


class CompactEvalDataset(BaseModel):
    name: str
    cases: list[CompactEvalCase] = Field(default_factory=list)

    @classmethod
    def load_yaml(cls, path: Path) -> CompactEvalDataset:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)


def _resp(name: str, args: dict[str, Any], call_id: str) -> ModelResponse:
    action = name if name in {"submit_finding", "finish_review"} else "tool_call"
    req = AgentToolRequest(id=call_id, name=name, arguments=args)
    return ModelResponse(action=action, data={"name": name, "arguments": args}, tool_requests=[req])


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


def _synth_session() -> AgentSession:
    task = ReviewTask(
        task_id="eval-c:src/a.py:agent",
        run_id="eval-c",
        kind=ReviewTaskKind.AGENT_TASK,
        target="src/a.py",
    )
    session = AgentSession(
        task,
        file_path="src/a.py",
        budget=AgentBudget(compact_threshold_ratio=0.4, compact_keep_rounds=1, max_session_chars=1500),
    )
    session.add_user("MIN_L3_UNIQUE")
    for index in range(4):
        call_id = f"c{index}"
        session.add_assistant(
            "",
            tool_calls=[
                AgentToolRequest(id=call_id, name="read_file", arguments={"path": "src/a.py", "start_line": 1})
            ],
        )
        session.add_tool_observation(tool_call_id=call_id, name="read_file", content="x" * 400)
        session.index_success(
            tool_call_id=call_id,
            name="read_file",
            arguments={"path": "src/a.py", "start_line": 1, "max_lines": 2},
            location=f"tool:read_file:{call_id}",
        )
    maybe_compact(session)
    return session


def _run_loop(
    script: list[ModelResponse],
    *,
    budget_kwargs: dict[str, Any],
    protocol: str = "native",
    user_text: str = "review src/a.py",
) -> tuple[AgentSession, FakeLLMProvider]:
    task = ReviewTask(
        task_id="eval-c:src/a.py:agent",
        run_id="eval-c",
        kind=ReviewTaskKind.AGENT_TASK,
        target="src/a.py",
    )
    session = AgentSession(task, file_path="src/a.py", budget=AgentBudget(**budget_kwargs))
    session.add_user(user_text)
    store = SqliteStorage(":memory:")
    asyncio.run(store.record_run(ReviewRun(run_id="eval-c")))
    blob = "\n".join(f"line-{i:04d} " + ("body" * 20) for i in range(80))
    workspace = ToolWorkspace(
        repo_root=None,
        snapshot=MemoryToolSnapshot({"src/a.py": blob + "\n"}, head_sha="head"),
        task_id=task.task_id,
        run_id="eval-c",
    )
    llm = FakeLLMProvider(tool_script=script)
    budget = GlobalBudget()
    asyncio.run(
        run_agent_loop(
            session,
            llm=llm,
            registry=builtin_registry(),
            workspace=workspace,
            global_budget=budget,
            coordinator=FinalizeBudgetCoordinator(budget),
            store=store,
            protocol=protocol,
            save_tool_trace=True,
            logger=None,
            model_sem=asyncio.Semaphore(1),
            max_output_tokens=64,
        )
    )
    return session, llm


def _eval_case(case: CompactEvalCase) -> dict[str, Any]:
    ok = False
    status = ""
    detail = ""
    if case.id == "compact-keeps-min-l3":
        session = _synth_session()
        initial = next(msg for msg in session.messages if msg.role == "user" and not msg.is_compressed)
        ok = initial.content == "MIN_L3_UNIQUE" and sum(msg.is_compressed for msg in session.messages) == 1
        status = "kept"
    elif case.id == "compact-keeps-evidence-ids":
        session, _llm = _run_loop(
            [
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
            ],
            budget_kwargs=_LOOP_BUDGET,
        )
        ok = (
            "tc-0" in session.evidence_index
            and bool(session.candidates)
            and any(msg.is_compressed for msg in session.messages)
        )
        status = session.status.value
    elif case.id == "compact-not-a-round":
        session, llm = _run_loop(
            [
                _resp("read_file", {"path": "src/a.py", "start_line": 1, "max_lines": 40}, "tc-0"),
                _resp("read_file", {"path": "src/a.py", "start_line": 41, "max_lines": 40}, "tc-1"),
                _resp("finish_review", {"reason": "done"}, "tc-fin"),
            ],
            budget_kwargs=_LOOP_BUDGET,
        )
        ok = len(llm.calls) == 3 and any(msg.is_compressed for msg in session.messages)
        status = f"calls={len(llm.calls)}"
    elif case.id == "compact-native-pairing":
        session = _synth_session()
        ok = _pairing_ok(session)
        status = "paired" if ok else "dangling"
    elif case.id == "compact-recompact-keeps-old-facts":
        task = ReviewTask(
            task_id="eval-c:src/a.py:agent",
            run_id="eval-c",
            kind=ReviewTaskKind.AGENT_TASK,
            target="src/a.py",
        )
        session = AgentSession(
            task,
            file_path="src/a.py",
            budget=AgentBudget(compact_threshold_ratio=0.4, compact_keep_rounds=1, max_session_chars=1500),
        )
        session.add_user("review")
        for index, payload in enumerate(("x" * 400, "x" * 400)):
            call_id = f"c{index}"
            session.add_assistant(
                "",
                tool_calls=[
                    AgentToolRequest(id=call_id, name="read_file", arguments={"path": "src/a.py", "start_line": 1})
                ],
            )
            session.add_tool_observation(tool_call_id=call_id, name="read_file", content=payload)
        maybe_compact(session)
        for index, payload in enumerate(("y" * 400, "y" * 400), start=2):
            call_id = f"c{index}"
            session.add_assistant(
                "",
                tool_calls=[
                    AgentToolRequest(id=call_id, name="read_file", arguments={"path": "src/a.py", "start_line": 1})
                ],
            )
            session.add_tool_observation(tool_call_id=call_id, name="read_file", content=payload)
        maybe_compact(session)
        summaries = [msg for msg in session.messages if msg.is_compressed]
        text = summaries[0].content if summaries else ""
        ok = len(summaries) == 1 and "c0" in text and "c2" in text
        status = "merged" if ok else "lost"
    elif case.id == "compact-json-repair-keeps-repair-instruction":
        session, llm = _run_loop(
            [
                ModelResponse(action=None, data={"parse_error": "bad json"}),
                _resp("finish_review", {"reason": "done"}, "tc-fin"),
            ],
            budget_kwargs={
                "max_rounds": 8,
                "max_session_chars": 8000,
                "compact_threshold_ratio": 0.5,
                "compact_keep_rounds": 0,
            },
            protocol="action_json",
            user_text="review " + ("U" * 4500),
        )
        ok = (
            any(
                msg.role == "user" and not msg.is_compressed and JSON_REPAIR_HINT in msg.content
                for msg in session.messages
            )
            and any(msg.is_compressed for msg in session.messages)
            and len(llm.calls) == 2
        )
        status = session.status.value
    elif case.id == "overflow-after-compact":
        task = ReviewTask(
            task_id="eval-c:src/a.py:agent",
            run_id="eval-c",
            kind=ReviewTaskKind.AGENT_TASK,
            target="src/a.py",
        )
        session = AgentSession(task, file_path="src/a.py", budget=AgentBudget(max_session_chars=400))
        session.add_user("Q" * 5000)
        store = SqliteStorage(":memory:")
        asyncio.run(store.record_run(ReviewRun(run_id="eval-c")))
        llm = FakeLLMProvider(tool_script=[_resp("finish_review", {"reason": "x"}, "fin")])
        budget = GlobalBudget()
        asyncio.run(
            run_agent_loop(
                session,
                llm=llm,
                registry=builtin_registry(),
                workspace=ToolWorkspace(
                    repo_root=None,
                    snapshot=MemoryToolSnapshot({"src/a.py": "x\n"}, head_sha="head"),
                    task_id=task.task_id,
                    run_id="eval-c",
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
        )
        ok = session.stop_reason == "context_overflow" and llm.calls == []
        status = session.status.value
    elif case.id == "default-agent-off":
        fake = FakeGitProvider()
        fake.add_snapshot("main", {"src/a.py": "a=1\n"})
        fake.add_snapshot("feat", {"src/a.py": "a=2\n"})
        fake.add_pr(1, base="main", head="feat")
        store = SqliteStorage(":memory:")
        service = ReviewService(fake, FakeLLMProvider(default_findings=[]), store)
        asyncio.run(service.review("1"))
        n = store._query("SELECT COUNT(*) AS n FROM tool_calls")[0]["n"]  # noqa: SLF001
        ok = n == 0 and service.settings.agent.enabled is False
        status = "ok" if ok else "error"
    else:
        detail = "unknown_case"
    return {"id": case.id, "kind": case.kind, "ok": ok, "status": status, "detail": detail}


def run_compare(dataset_path: str | Path = _DEFAULT_DATASET) -> dict[str, Any]:
    dataset = CompactEvalDataset.load_yaml(Path(dataset_path))
    rows = [_eval_case(case) for case in dataset.cases]
    passed = sum(1 for row in rows if row["ok"])
    return {
        "dataset": dataset.name,
        "samples": len(rows),
        "real_api": False,
        "compact": {
            "passed": passed,
            "total": len(rows),
            "cases": rows,
            "note": "脚本化 Fake；压缩为程序生成。agent.enabled 默认 false。",
        },
        "all_passed": passed == len(rows),
    }


def render_markdown(report: dict[str, Any]) -> str:
    box = report["compact"]
    lines = [
        "# V3-C 会话压缩对照（脚本化 Fake，非真实 API）",
        "",
        "> 由 `python -m reposage.evals.v3c_compare` 生成；原始 JSON：`docs/evidence/v3-c-compare.json`。",
        f"> dataset=`{report['dataset']}` samples={report['samples']} real_api={report['real_api']}",
        "> 压缩由程序生成；OQ-11 live 后置。脱敏：不含 API Key / 源码原文。",
        "",
        "## 压缩 / 证据索引 / 隔离",
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
    print(f"passed={report['compact']['passed']}/{report['compact']['total']} all_passed={report['all_passed']}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
