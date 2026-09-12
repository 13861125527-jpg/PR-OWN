"""V3-A 工具信封与只读 handler。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from reposage.config.settings import Settings
from reposage.domain.diff import parse_unified_diff
from reposage.domain.enums import ToolCallStatus
from reposage.domain.models import ToolDefinition
from reposage.providers.git.fake import FakeGitProvider
from reposage.providers.llm.fake import FakeLLMProvider
from reposage.review.service import ReviewService
from reposage.storage.sqlite import SqliteStorage
from reposage.tools import builtin_registry, invoke, is_repeat
from reposage.tools.registry import HandlerOutput, ToolRegistry
from reposage.tools.snapshot import GitToolSnapshot, MemoryToolSnapshot, ToolWorkspace


def test_tool_definition_rejects_tiny_result_limit():
    with pytest.raises(ValidationError):
        ToolDefinition(name="x", description="y", max_result_chars=64)


def _ws(
    blobs: dict[str, str],
    *,
    repo_root: Path | None = None,
    head_sha: str = "head",
    diff: str | None = None,
) -> ToolWorkspace:
    files = {f.path: f for f in parse_unified_diff(diff)} if diff else {}
    return ToolWorkspace(
        repo_root=repo_root or Path("missing-root"),
        snapshot=MemoryToolSnapshot(blobs, head_sha=head_sha, repository_id="r"),
        diff_files=files,
    )


@pytest.mark.asyncio
async def test_unknown_tool_and_schema():
    registry = builtin_registry()
    ws = _ws({"src/a.py": "x = 1\n"})
    call, result = await invoke(registry, ws, "nope", {})
    assert call.status is ToolCallStatus.INVALID_ARGS
    assert result.error == "unknown_tool"
    call, result = await invoke(registry, ws, "read_file", {})
    assert call.status is ToolCallStatus.INVALID_ARGS
    call, result = await invoke(
        registry, ws, "read_file", {"path": "src/a.py", "path2": "x"}
    )
    assert call.status is ToolCallStatus.INVALID_ARGS


@pytest.mark.asyncio
async def test_path_attacks_rejected(tmp_path: Path):
    registry = builtin_registry()
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = ToolWorkspace(
        repo_root=repo,
        snapshot=MemoryToolSnapshot({"src/a.py": "ok\n"}, head_sha="h"),
    )
    for args in (
        {"path": "/etc/passwd"},
        {"path": "../secret"},
        {"path": "C:/Windows/win.ini"},
        {"path": ""},
    ):
        call, result = await invoke(registry, ws, "read_file", args)
        assert call.status is ToolCallStatus.INVALID_ARGS
        assert result.error == "invalid_path"


@pytest.mark.asyncio
async def test_read_file_from_snapshot_not_workdir(tmp_path: Path):
    registry = builtin_registry()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("LIVE\n", encoding="utf-8")
    ws = ToolWorkspace(
        repo_root=repo,
        snapshot=MemoryToolSnapshot({"src/a.py": "SNAP\nline2\n"}, head_sha="deadbeef"),
    )
    call, result = await invoke(registry, ws, "read_file", {"path": "src/a.py"})
    assert call.status is ToolCallStatus.OK
    assert result.truncated is False
    body = json.loads(result.data or "")
    assert body["payload"]["lines"] == ["SNAP", "line2"]
    assert body["source"]["kind"] == "tool"
    assert body["source"]["sha"] == "deadbeef"
    assert result.source is not None
    assert result.source.sha == "deadbeef"


@pytest.mark.asyncio
async def test_read_file_truncates_lines():
    registry = builtin_registry()
    lines = "\n".join(f"L{i}" for i in range(1, 250))
    ws = _ws({"src/a.py": lines + "\n"})
    call, result = await invoke(
        registry, ws, "read_file", {"path": "src/a.py", "max_lines": 200}
    )
    assert call.status is ToolCallStatus.OK
    assert result.truncated is True
    body = json.loads(result.data or "")
    assert len(body["payload"]["lines"]) == 200


@pytest.mark.asyncio
async def test_find_files_and_search_literal():
    registry = builtin_registry()
    ws = _ws(
        {
            "src/a.py": "value = foo.bar()\n",
            "src/b.py": "foo = 1\n",
            "docs/n.md": "foo.bar is regex-looking\n",
        }
    )
    call, result = await invoke(registry, ws, "find_files", {"pattern": "src/**/*.py"})
    assert json.loads(result.data or "")["payload"]["paths"] == ["src/a.py", "src/b.py"]
    _call, result = await invoke(registry, ws, "search_code", {"query": "foo.bar"})
    hits = json.loads(result.data or "")["payload"]["hits"]
    assert {h["path"] for h in hits} == {"src/a.py", "docs/n.md"}
    _call, result = await invoke(
        registry, ws, "search_code", {"query": "foo.bar", "scope": "src/**"}
    )
    hits = json.loads(result.data or "")["payload"]["hits"]
    assert [h["path"] for h in hits] == ["src/a.py"]
    _call, result = await invoke(registry, ws, "search_code", {"query": "foo.*"})
    assert json.loads(result.data or "")["payload"]["hits"] == []
    call, result = await invoke(registry, ws, "search_code", {"query": ""})
    assert call.status is ToolCallStatus.INVALID_ARGS
    call, result = await invoke(registry, ws, "search_code", {"query": "x" * 257})
    assert call.status is ToolCallStatus.INVALID_ARGS


@pytest.mark.asyncio
async def test_search_code_scan_caps():
    registry = builtin_registry()
    blobs = {f"f{i}.py": "needle here\n" for i in range(120)}
    ws = _ws(blobs)
    _call, result = await invoke(registry, ws, "search_code", {"query": "needle"})
    assert result.truncated is True
    hits = json.loads(result.data or "")["payload"]["hits"]
    assert len(hits) <= 50


@pytest.mark.asyncio
async def test_find_references_char_caps():
    from reposage.tools.find_references import MAX_CHARS_PER_FILE, MAX_TOTAL_CHARS_SCANNED

    registry = builtin_registry()
    prefix = "def target():\n    return 1\n"
    filler = "a" * (MAX_CHARS_PER_FILE + 1000)
    suffix = "\nvalue = target()\n"
    ws = _ws({"src/a.py": prefix + filler + suffix})
    _call, result = await invoke(registry, ws, "find_references", {"symbol": "target"})
    assert result.truncated is True
    refs = json.loads(result.data or "")["payload"]["refs"]
    assert any(r["kind"] == "function" and r["line"] == 1 for r in refs)
    assert not any(r["kind"] == "ref" and r["line"] > 3 for r in refs)

    blobs = {
        f"f{i:03d}.py": "def target():\n    pass\n" + ("b" * 15_000) for i in range(20)
    }
    ws = _ws(blobs)
    _call, result = await invoke(registry, ws, "find_references", {"symbol": "target"})
    assert result.truncated is True
    refs = json.loads(result.data or "")["payload"]["refs"]
    scanned_files = {r["path"] for r in refs}
    assert len(scanned_files) < 20
    assert sum(len(blobs[p]) for p in scanned_files) <= MAX_TOTAL_CHARS_SCANNED + 15_000


@pytest.mark.asyncio
async def test_find_references_and_read_diff():
    registry = builtin_registry()
    src = "def target():\n    return 1\n\ndef other():\n    return target()\n"
    diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,1 +1,2 @@\n"
        "-old\n"
        "+def target():\n"
        "+    return 1\n"
    )
    ws = _ws({"src/a.py": src}, diff=diff)
    call, result = await invoke(registry, ws, "find_references", {"symbol": "target"})
    assert call.status is ToolCallStatus.OK
    refs = json.loads(result.data or "")["payload"]["refs"]
    assert any(r["kind"] == "function" for r in refs)
    _call, result = await invoke(registry, ws, "find_references", {"symbol": "missing"})
    assert json.loads(result.data or "")["payload"]["refs"] == []
    call, result = await invoke(registry, ws, "read_diff", {"path": "src/a.py"})
    assert call.status is ToolCallStatus.OK
    assert json.loads(result.data or "")["payload"]["hunks"]
    call, result = await invoke(registry, ws, "read_diff", {"path": "src/missing.py"})
    assert call.status is ToolCallStatus.ERROR
    assert result.error == "not_found"


@pytest.mark.asyncio
async def test_stubs_not_bound():
    registry = builtin_registry()
    ws = _ws({"src/a.py": "x\n"})
    call, result = await invoke(
        registry,
        ws,
        "submit_finding",
        {
            "title": "t",
            "severity": "high",
            "confidence": 0.9,
            "category": "security",
        },
    )
    assert call.status is ToolCallStatus.ERROR
    assert result.error == "not_bound"
    _call, result = await invoke(registry, ws, "finish_review", {"reason": "done"})
    assert result.error == "not_bound"


@pytest.mark.asyncio
async def test_timeout_and_cancel():
    class Empty(BaseModel):
        model_config = ConfigDict(extra="forbid")

    async def sleepy(_ws: ToolWorkspace, _args: BaseModel) -> HandlerOutput:
        await asyncio.sleep(5)
        return HandlerOutput(payload={"late": True})

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(name="slow", description="x", timeout_s=1, parameters={}),
        Empty,
        sleepy,
    )
    ws = _ws({"src/a.py": "x\n"})
    call, result = await invoke(registry, ws, "slow", {})
    assert call.status is ToolCallStatus.TIMEOUT
    assert result.error == "timeout"

    from reposage.domain.enums import ReviewTaskKind, ReviewTaskStatus
    from reposage.domain.run import ReviewRun, ReviewTask

    store = SqliteStorage(":memory:")
    await store.record_run(ReviewRun(run_id="run-c", head_sha="h"))
    await store.record_tasks(
        [
            ReviewTask(
                task_id="task-c",
                run_id="run-c",
                kind=ReviewTaskKind.FILE_REVIEW,
                target="src/a.py",
                status=ReviewTaskStatus.RUNNING,
            )
        ]
    )
    ws.task_id = "task-c"
    task = asyncio.create_task(
        invoke(registry, ws, "slow", {}, store=store, save_tool_trace=True, tool_call_id="tc-cancel")
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = store._query("SELECT status FROM tool_calls WHERE tool_call_id='tc-cancel'")[0]  # noqa: SLF001
    assert row["status"] == ToolCallStatus.CANCELLED.value

    class BoomStore:
        async def record_tool_invocation(self, *_a: object, **_k: object) -> None:
            raise RuntimeError("audit down")

    task = asyncio.create_task(
        invoke(
            registry,
            ws,
            "slow",
            {},
            store=BoomStore(),  # type: ignore[arg-type]
            save_tool_trace=True,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_git_adapter_and_repeat():
    fake = FakeGitProvider()
    fake.add_snapshot("head", {"src/a.py": "alpha\n"})
    snap = GitToolSnapshot(fake, head_sha="head", repository_id="fake")
    assert await snap.list_paths() == ["src/a.py"]
    assert await snap.get_blob("src/a.py") == "alpha\n"
    assert await snap.get_blob("../x") is None
    assert is_repeat("read_file", {"path": "src/a.py"}, []) is None


@pytest.mark.asyncio
async def test_service_rejects_agentic():
    settings = Settings.model_validate({"review": {"strategy": "agentic"}})
    with pytest.raises(ValueError, match="agentic"):
        ReviewService(
            FakeGitProvider(),
            FakeLLMProvider(),
            SqliteStorage(":memory:"),
            settings=settings,
        )


@pytest.mark.asyncio
async def test_default_review_writes_no_tool_calls():
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/a.py": "def f():\n    return 1\n"})
    fake.add_snapshot("feat", {"src/a.py": "def f():\n    return 2\n"})
    fake.add_pr(1, base="main", head="feat")
    store = SqliteStorage(":memory:")
    service = ReviewService(fake, FakeLLMProvider(default_findings=[]), store)
    await service.review("1")
    n = store._query("SELECT COUNT(*) AS n FROM tool_calls")[0]["n"]  # noqa: SLF001
    assert n == 0
    assert service.settings.agent.enabled is False


@pytest.mark.asyncio
async def test_invoke_persist_trace_switch_and_redact(tmp_path: Path):
    from reposage.domain.enums import ReviewTaskKind, ReviewTaskStatus
    from reposage.domain.run import ReviewRun, ReviewTask

    store = SqliteStorage(tmp_path / "t.db")
    await store.record_run(ReviewRun(run_id="run-1", head_sha="h"))
    await store.record_tasks(
        [
            ReviewTask(
                task_id="task-1",
                run_id="run-1",
                kind=ReviewTaskKind.FILE_REVIEW,
                target="src/a.py",
                status=ReviewTaskStatus.RUNNING,
            )
        ]
    )
    registry = builtin_registry()
    ws = _ws({"src/a.py": "ok\n"})
    ws.task_id = "task-1"
    await invoke(
        registry,
        ws,
        "read_file",
        {"path": "src/a.py"},
        store=store,
        save_tool_trace=False,
        tool_call_id="tc-off",
    )
    assert store._query("SELECT COUNT(*) AS n FROM tool_calls")[0]["n"] == 0  # noqa: SLF001
    call, _result = await invoke(
        registry,
        ws,
        "read_file",
        {"path": "src/a.py"},
        store=store,
        save_tool_trace=True,
        tool_call_id="tc-on",
    )
    assert call.status is ToolCallStatus.OK
    assert store._query("SELECT COUNT(*) AS n FROM tool_calls")[0]["n"] == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_invoke_invalid_args_huge_payload_is_capped():
    from reposage.domain.enums import ReviewTaskKind, ReviewTaskStatus
    from reposage.domain.run import ReviewRun, ReviewTask
    from reposage.observability.tool_trace import AUDIT_ARGS_MAX_CHARS

    store = SqliteStorage(":memory:")
    await store.record_run(ReviewRun(run_id="run-1", head_sha="h"))
    await store.record_tasks(
        [
            ReviewTask(
                task_id="task-1",
                run_id="run-1",
                kind=ReviewTaskKind.FILE_REVIEW,
                target="src/a.py",
                status=ReviewTaskStatus.RUNNING,
            )
        ]
    )
    registry = builtin_registry()
    ws = _ws({"src/a.py": "ok\n"})
    ws.task_id = "task-1"
    call, result = await invoke(
        registry,
        ws,
        "read_file",
        {"path": "src/a.py", "pad": "x" * 20_000},
        store=store,
        save_tool_trace=True,
        tool_call_id="tc-huge",
    )
    assert call.status is ToolCallStatus.INVALID_ARGS
    row = store._query(  # noqa: SLF001
        "SELECT c.args_json, r.truncated FROM tool_calls c "
        "JOIN tool_results r ON r.tool_call_id=c.tool_call_id WHERE c.tool_call_id='tc-huge'"
    )[0]
    assert len(row["args_json"]) <= AUDIT_ARGS_MAX_CHARS
    parsed = json.loads(row["args_json"])
    assert parsed["_audit_truncated"] is True
    assert row["truncated"] == 1
    assert result.error == "invalid_args"
