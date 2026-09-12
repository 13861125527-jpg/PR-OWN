"""SQLite 基础存储测试（P0-1/P0-2/P1-4/P1-5 回归）。"""

import json
import sqlite3

import pytest
from reposage.domain.enums import FindingCategory, Severity
from reposage.domain.finding import Finding, FindingStatus
from reposage.domain.models import (
    Evidence,
    EvidenceKind,
    FindingSource,
    FindingSourceKind,
    ModelUsage,
    ModelUsageOutcome,
)
from reposage.domain.run import GateDecision, ReviewRun
from reposage.storage.sqlite import SqliteStorage


@pytest.fixture
def store(tmp_path):
    s = SqliteStorage(tmp_path / "test.db")
    yield s
    s.close()


def test_foreign_keys_enabled(store: SqliteStorage):
    row = store._query("PRAGMA foreign_keys")  # noqa: SLF001
    assert row[0][0] == 1


def test_schema_initialized(store: SqliteStorage):
    tables = {r[0] for r in store._query("SELECT name FROM sqlite_master WHERE type='table'")}
    for expected in (
        "runs",
        "findings",
        "finding_versions",
        "usages",
        "coverages",
        "tool_calls",
        "tool_results",
        "publish_plans",
        "publish_operations",
        "published_comments",
        "feedback",
        "pr_caches",
        "gate_decisions",
    ):
        assert expected in tables


def test_schema_publish_columns(store: SqliteStorage):
    """P2（复验）：发布模型列必须存在。"""
    comments_cols = {r[1] for r in store._query("PRAGMA table_info(published_comments)")}
    assert {"comment_id", "required", "finding_occurrence_id", "remote_comment_id", "marker"} <= comments_cols
    ops_cols = {r[1] for r in store._query("PRAGMA table_info(publish_operations)")}
    assert {"op_id", "plan_id", "kind", "status", "detail"} <= ops_cols
    usage_cols = {r[1] for r in store._query("PRAGMA table_info(usages)")}
    assert {"schema_hash", "schema_repairs"} <= usage_cols
    finding_cols = {r[1] for r in store._query("PRAGMA table_info(findings)")}
    assert "needs_evidence" in finding_cols


@pytest.mark.asyncio
async def test_record_and_read_run(store: SqliteStorage):
    run = ReviewRun(run_id="run-1", base_sha="a" * 7, head_sha="b" * 7, warnings=["w1", "w2"])
    await store.record_run(run)

    row = store._query("SELECT * FROM runs WHERE run_id='run-1'")[0]  # noqa: SLF001
    assert row["head_sha"] == "b" * 7
    # P1-4：warnings_json 只存 warnings 列表，不是整个 run
    assert json.loads(row["warnings_json"]) == ["w1", "w2"]


@pytest.mark.asyncio
async def test_record_run_persists_stages(store: SqliteStorage):
    """V1-f：run_stages 落库阶段记录（stage/status/required/duration/tokens/cost/error/detail）。"""
    from reposage.domain.enums import StageName, StageStatus
    from reposage.domain.run import StageResult

    run = ReviewRun(run_id="run-1", base_sha="a" * 7, head_sha="b" * 7)
    run.stages = [
        StageResult(stage=StageName.PREFLIGHT, status=StageStatus.OK, duration_ms=1),
        StageResult(stage=StageName.FETCH, status=StageStatus.OK, detail="head=x", duration_ms=2),
        StageResult(stage=StageName.REVIEW, status=StageStatus.PARTIAL, tokens=100, cost_usd=0.5, error="e"),
    ]
    await store.record_run(run)

    rows = store._query("SELECT * FROM run_stages WHERE run_id='run-1' ORDER BY sequence")
    assert [r["stage"] for r in rows] == ["preflight", "fetch", "review"]
    assert rows[1]["detail"] == "head=x"
    assert rows[2]["tokens"] == 100
    assert rows[2]["cost_usd"] == 0.5
    assert rows[2]["error"] == "e"


@pytest.mark.asyncio
async def test_record_run_stages_idempotent_no_growth(store: SqliteStorage):
    """V1-f：同 run 重写（第一次落库 + finish 后定稿）不产生无界重复 stages。"""
    from reposage.domain.enums import StageName, StageStatus
    from reposage.domain.run import StageResult

    run = ReviewRun(run_id="run-1", base_sha="a" * 7, head_sha="b" * 7)
    run.stages = [
        StageResult(stage=StageName.PREFLIGHT, status=StageStatus.OK),
        StageResult(stage=StageName.FETCH, status=StageStatus.OK),
    ]
    await store.record_run(run)
    assert len(store._query("SELECT id FROM run_stages WHERE run_id='run-1'")) == 2

    # finish 后追加 PUBLISH 阶段再写一次 → 仍只保留最新 stages（3 条，非 5 条）
    run.stages.append(StageResult(stage=StageName.PUBLISH, status=StageStatus.OK))
    await store.record_run(run)
    rows = store._query("SELECT * FROM run_stages WHERE run_id='run-1' ORDER BY sequence")
    assert len(rows) == 3
    assert [r["stage"] for r in rows] == ["preflight", "fetch", "publish"]


@pytest.mark.asyncio
async def test_record_run_stages_same_stage_repeated_preserves_order(store: SqliteStorage):
    """V1-f：同 stage 多次出现（如旧计划 cleanup + 当前计划 publish）顺序可还原。"""
    from reposage.domain.enums import StageName, StageStatus
    from reposage.domain.run import StageResult

    run = ReviewRun(run_id="run-1", base_sha="a" * 7, head_sha="b" * 7)
    run.stages = [
        StageResult(stage=StageName.PUBLISH, status=StageStatus.PARTIAL, detail="cleanup"),
        StageResult(stage=StageName.PUBLISH, status=StageStatus.OK, detail="publish"),
    ]
    await store.record_run(run)
    rows = store._query("SELECT stage, sequence, detail FROM run_stages WHERE run_id='run-1' ORDER BY sequence")
    assert len(rows) == 2
    assert rows[0]["detail"] == "cleanup"
    assert rows[1]["detail"] == "publish"


@pytest.mark.asyncio
async def test_record_finding_and_versions(store: SqliteStorage):
    await store.record_run(ReviewRun(run_id="run-1", base_sha="a" * 7, head_sha="b" * 7))
    finding = Finding(
        finding_occurrence_id="occ-1",
        run_id="run-1",
        fingerprint="fp-1",
        cross_run_match_key="key-1",
        title="t",
        severity=Severity.HIGH,
        confidence=0.9,
        category=FindingCategory.SECURITY,
        canonical_path="src/a.py",
        canonical_start_line=10,
        is_outside_diff=False,
        evidence=[Evidence(kind=EvidenceKind.DIFF_LINE, location="src/a.py:10", content="x")],
        sources=[FindingSource(kind=FindingSourceKind.LLM_GENERAL)],
    )
    finding.record_transition(FindingStatus.SCHEMA_VALID, reason="ok")
    await store.record_findings([finding])

    row = store._query("SELECT * FROM findings WHERE finding_occurrence_id='occ-1'")[0]  # noqa: SLF001
    assert row["canonical_path"] == "src/a.py"
    assert row["is_outside_diff"] == 0  # bool 落库为整数
    assert row["needs_evidence"] == 0
    # P1-5：evidence/sources 分别序列化对应列表
    assert json.loads(row["evidence_json"])[0]["location"] == "src/a.py:10"
    assert json.loads(row["sources_json"])[0]["kind"] == "llm_general"
    # finding_versions 已持久化
    versions = store._query(  # noqa: SLF001
        "SELECT * FROM finding_versions WHERE finding_occurrence_id='occ-1'"
    )
    assert len(versions) == 1
    assert versions[0]["to_status"] == "schema_valid"


@pytest.mark.asyncio
async def test_record_usage_late_cancelled(store: SqliteStorage):
    """P0-R2-3：迟到响应的 usage 仍记账。"""
    await store.record_run(ReviewRun(run_id="run-1", base_sha="a" * 7, head_sha="b" * 7))
    usage = ModelUsage(
        model="m",
        role="general",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.01,
        outcome=ModelUsageOutcome.LATE_CANCELLED,
    )
    await store.record_usage("run-1", usage)

    row = store._query("SELECT * FROM usages")[0]  # noqa: SLF001
    assert row["outcome"] == "late_cancelled"
    assert row["cost_usd"] == 0.01


@pytest.mark.asyncio
async def test_record_tasks_and_coverage(store: SqliteStorage):
    """V1-f：ReviewTask（成本/Token）与 CoverageManifest（CoverageItem）落库。"""
    from reposage.domain.enums import CoverageReason, ReviewTaskKind, ReviewTaskStatus, StageName
    from reposage.domain.models import CoverageItem, CoverageManifest
    from reposage.domain.run import ReviewTask

    await store.record_run(ReviewRun(run_id="run-1", base_sha="a" * 7, head_sha="b" * 7))

    task = ReviewTask(
        task_id="src/a.py",
        run_id="run-1",
        kind=ReviewTaskKind.FILE_REVIEW,
        target="src/a.py",
        status=ReviewTaskStatus.COMPLETED,
        input_tokens=120,
        output_tokens=45,
        cost_usd=0.03,
    )
    await store.record_tasks([task])
    task_row = store._query("SELECT * FROM tasks WHERE task_id='src/a.py'")[0]
    assert task_row["in_tokens"] == 120
    assert task_row["out_tokens"] == 45
    assert task_row["cost_usd"] == 0.03
    assert task_row["status"] == "completed"

    coverage = CoverageManifest(
        items=[
            CoverageItem(target="src/a.py", reason=CoverageReason.COVERED, stage=StageName.CONTEXT),
            CoverageItem(target="src/skip.py", reason=CoverageReason.SKIPPED_LANG, stage=StageName.FETCH, detail="binary"),
        ],
        truncated=True,
    )
    await store.record_coverage("run-1", coverage)
    cov_row = store._query("SELECT * FROM coverages WHERE run_id='run-1'")[0]
    assert cov_row["truncated"] == 1
    items = json.loads(cov_row["items_json"])
    assert len(items) == 2
    assert items[0]["target"] == "src/a.py"
    assert items[1]["reason"] == "skipped_lang"


@pytest.mark.asyncio
async def test_orphan_task_rejected(store: SqliteStorage):
    """V1-f：tasks 引用不存在的 run 必须抛 IntegrityError（外键）。"""
    from reposage.domain.enums import ReviewTaskKind, ReviewTaskStatus
    from reposage.domain.run import ReviewTask

    task = ReviewTask(
        task_id="t1", run_id="no-such-run", kind=ReviewTaskKind.FILE_REVIEW,
        target="x", status=ReviewTaskStatus.PENDING,
    )
    with pytest.raises(sqlite3.IntegrityError):
        await store.record_tasks([task])


@pytest.mark.asyncio
async def test_tasks_across_runs_same_path_not_colliding(store: SqliteStorage):
    """V1-f（复验 blocking）：跨 run 审同一文件，task_id 全局唯一不撞键，旧 run 成本/Token 保留。"""
    from reposage.domain.enums import ReviewTaskKind, ReviewTaskStatus
    from reposage.domain.run import ReviewTask

    await store.record_run(ReviewRun(run_id="run-1", base_sha="a" * 7, head_sha="b" * 7))
    await store.record_run(ReviewRun(run_id="run-2", base_sha="a" * 7, head_sha="c" * 7))

    t1 = ReviewTask(
        task_id="run-1:src/a.py", run_id="run-1", kind=ReviewTaskKind.FILE_REVIEW,
        target="src/a.py", status=ReviewTaskStatus.COMPLETED,
        input_tokens=100, output_tokens=40, cost_usd=0.02,
    )
    t2 = ReviewTask(
        task_id="run-2:src/a.py", run_id="run-2", kind=ReviewTaskKind.FILE_REVIEW,
        target="src/a.py", status=ReviewTaskStatus.COMPLETED,
        input_tokens=200, output_tokens=80, cost_usd=0.05,
    )
    await store.record_tasks([t1])
    await store.record_tasks([t2])

    rows = store._query("SELECT run_id, in_tokens, cost_usd FROM tasks ORDER BY run_id")
    assert len(rows) == 2  # 两行都保留，未撞键覆盖
    by_run = {r["run_id"]: r for r in rows}
    assert by_run["run-1"]["in_tokens"] == 100
    assert by_run["run-1"]["cost_usd"] == 0.02
    assert by_run["run-2"]["in_tokens"] == 200
    assert by_run["run-2"]["cost_usd"] == 0.05


@pytest.mark.asyncio
async def test_duplicate_fingerprint_rejected(store: SqliteStorage):
    """UNIQUE(run_id, fingerprint)：同 run 同指纹第二次插入抛 IntegrityError。"""
    await store.record_run(ReviewRun(run_id="run-1", base_sha="a" * 7, head_sha="b" * 7))

    def mk(occ: str, fp: str) -> Finding:
        return Finding(
            finding_occurrence_id=occ,
            run_id="run-1",
            fingerprint=fp,
            cross_run_match_key="k",
            title="t",
            severity=Severity.MEDIUM,
            confidence=0.8,
            category=FindingCategory.CORRECTNESS,
        )

    await store.record_findings([mk("occ-1", "fp")])
    with pytest.raises(sqlite3.IntegrityError):
        await store.record_findings([mk("occ-2", "fp")])


@pytest.mark.asyncio
async def test_orphan_finding_rejected(store: SqliteStorage):
    """P0-2：外键开启后，引用不存在 run 的 finding 必须抛 IntegrityError。"""
    finding = Finding(
        finding_occurrence_id="occ-x",
        run_id="no-such-run",
        fingerprint="fp",
        cross_run_match_key="k",
        title="t",
        severity=Severity.LOW,
        confidence=0.5,
        category=FindingCategory.CORRECTNESS,
    )
    with pytest.raises(sqlite3.IntegrityError):
        await store.record_findings([finding])


@pytest.mark.asyncio
async def test_orphan_usage_rejected(store: SqliteStorage):
    """P0-2：usages 引用不存在的 run 必须抛 IntegrityError。"""
    usage = ModelUsage(model="m", role="r", input_tokens=1, output_tokens=1, cost_usd=0.0)
    with pytest.raises(sqlite3.IntegrityError):
        await store.record_usage("no-such-run", usage)


@pytest.mark.asyncio
async def test_batch_findings_rollback_on_failure(store: SqliteStorage):
    """P1（复验）：批量写中途失败必须整体回滚，不能留下半批数据。"""
    await store.record_run(ReviewRun(run_id="run-1", base_sha="a" * 7, head_sha="b" * 7))

    def mk(occ: str, fp: str) -> Finding:
        return Finding(
            finding_occurrence_id=occ,
            run_id="run-1",
            fingerprint=fp,
            cross_run_match_key="k",
            title="t",
            severity=Severity.MEDIUM,
            confidence=0.8,
            category=FindingCategory.CORRECTNESS,
        )

    # 第一条 fp="dup" 成功插入，第二条同 fp 违反 UNIQUE(run_id, fingerprint) → 整批回滚
    with pytest.raises(sqlite3.IntegrityError):
        await store.record_findings([mk("occ-a", "dup"), mk("occ-b", "dup")])

    # 半批数据不得残留
    rows = store._query("SELECT * FROM findings WHERE run_id='run-1'")  # noqa: SLF001
    assert len(rows) == 0

    # 后续写其他记录也不得让第一条"复活"
    await store.record_usage(
        "run-1",
        ModelUsage(model="m", role="r", input_tokens=1, output_tokens=1, cost_usd=0.0),
    )
    rows = store._query("SELECT * FROM findings WHERE run_id='run-1'")  # noqa: SLF001
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_gate_decisions_upsert_idempotent_and_no_source(store: SqliteStorage):
    await store.record_run(ReviewRun(run_id="run-1", base_sha="a" * 7, head_sha="b" * 7))
    first = GateDecision(
        role_id="security",
        file_path="src/app.py",
        enabled=False,
        reason="gate_miss",
        matched_features=["exec_dyn"],
        gate_version="abc",
    )
    await store.record_gate_decisions("run-1", [first])
    second = first.model_copy(update={"enabled": True, "reason": "gate_hit"})
    await store.record_gate_decisions("run-1", [second])
    rows = store._query("SELECT * FROM gate_decisions")  # noqa: SLF001
    assert len(rows) == 1
    assert rows[0]["enabled"] == 1
    assert rows[0]["reason"] == "gate_hit"
    blob = rows[0]["matched_features_json"]
    assert "exec_dyn" in blob
    assert "eval(" not in blob
    assert "return eval" not in blob


@pytest.mark.asyncio
async def test_feedback_crud_revoke_replay_and_index(store: SqliteStorage):
    from reposage.domain.enums import FeedbackKind, RevokeFeedbackResult
    from reposage.domain.run import FeedbackMemory

    indexes = {r[1] for r in store._query("PRAGMA index_list(feedback)")}  # noqa: SLF001
    assert "idx_feedback_repo_active" in indexes
    mem = FeedbackMemory(
        repo="RepoSage",
        kind=FeedbackKind.FALSE_POSITIVE,
        path="src/a.py",
        category="security",
        rationale="fp",
    )
    fid = await store.record_feedback(mem)
    assert fid >= 1
    loaded = await store.get_finding("missing")
    assert loaded is None
    active = await store.load_active_feedback("RepoSage")
    assert len(active) == 1
    assert active[0].id == fid
    assert await store.revoke_feedback(fid, repo="RepoSage") == RevokeFeedbackResult.REVOKED
    assert await store.revoke_feedback(fid, repo="RepoSage") == RevokeFeedbackResult.ALREADY_REVOKED
    assert await store.load_active_feedback("RepoSage") == []
    all_rows = await store.list_feedback("RepoSage", include_revoked=True)
    assert len(all_rows) == 1
    assert all_rows[0].active is False
    assert all_rows[0].revoked_at is not None
    remaining = store._query("SELECT COUNT(*) AS n FROM feedback")  # noqa: SLF001
    assert remaining[0]["n"] == 1


@pytest.mark.asyncio
async def test_revoke_feedback_missing_and_wrong_repo(store: SqliteStorage):
    from reposage.domain.enums import FeedbackKind, RevokeFeedbackResult
    from reposage.domain.run import FeedbackMemory

    other = FeedbackMemory(
        repo="OtherProject",
        kind=FeedbackKind.FALSE_POSITIVE,
        path="src/a.py",
        rationale="other",
    )
    other_id = await store.record_feedback(other)
    assert await store.revoke_feedback(999, repo="RepoSage") == RevokeFeedbackResult.NOT_FOUND
    assert await store.revoke_feedback(other_id, repo="RepoSage") == RevokeFeedbackResult.WRONG_REPO
    still = await store.load_active_feedback("OtherProject")
    assert len(still) == 1
    assert still[0].id == other_id
    assert still[0].active is True


@pytest.mark.asyncio
async def test_get_finding_and_committed_watermark(store: SqliteStorage):
    from reposage.domain.enums import PublishPlanStatus
    from reposage.domain.run import PublishPlan

    await store.record_run(ReviewRun(run_id="run-1", base_sha="a" * 7, head_sha="b" * 7))
    finding = Finding(
        finding_occurrence_id="occ-1",
        run_id="run-1",
        fingerprint="fp",
        cross_run_match_key="k",
        title="t",
        severity=Severity.HIGH,
        confidence=0.9,
        category=FindingCategory.SECURITY,
        canonical_path="src/a.py",
        canonical_start_line=3,
        status=FindingStatus.ACCEPTED,
    )
    await store.record_findings([finding])
    got = await store.get_finding("occ-1")
    assert got is not None
    assert got.canonical_path == "src/a.py"
    assert got.category is FindingCategory.SECURITY
    plan = PublishPlan(
        plan_id="plan-1",
        run_id="run-1",
        pr_identity="RepoSage#1",
        mode="publish",
        status=PublishPlanStatus.COMPLETED,
        target_head_sha="head2",
        committed_watermark="wm-sha",
    )
    await store.record_publish_plan(plan)
    assert await store.load_committed_watermark("RepoSage#1") == "wm-sha"
    dry = PublishPlan(
        plan_id="plan-dry",
        run_id="run-1",
        pr_identity="RepoSage#1",
        mode="dry_run",
        status=PublishPlanStatus.PREPARED,
        target_head_sha="head3",
        committed_watermark=None,
    )
    await store.record_publish_plan(dry)
    assert await store.load_committed_watermark("RepoSage#1") == "wm-sha"


@pytest.mark.asyncio
async def test_record_tool_invocation_idempotent_conflict_and_redact(store: SqliteStorage):
    from reposage.domain.enums import ReviewTaskKind, ReviewTaskStatus, ToolCallStatus
    from reposage.domain.models import ToolCall, ToolResult
    from reposage.domain.run import ReviewTask

    await store.record_run(ReviewRun(run_id="run-t", head_sha="h"))
    await store.record_tasks(
        [
            ReviewTask(
                task_id="task-1",
                run_id="run-t",
                kind=ReviewTaskKind.FILE_REVIEW,
                target="src/a.py",
                status=ReviewTaskStatus.RUNNING,
            )
        ]
    )
    call = ToolCall(
        tool_call_id="tc-1",
        name="read_file",
        arguments={"path": "src/a.py", "token": "sk-abcdefghijklmnopqrstuv"},
        task_id="task-1",
        status=ToolCallStatus.OK,
    )
    result = ToolResult(tool_call_id="tc-1", data='{"payload":{"ok":true}}', error=None)
    await store.record_tool_invocation(call, result)
    await store.record_tool_invocation(call, result)
    row = store._query("SELECT args_json FROM tool_calls WHERE tool_call_id='tc-1'")[0]  # noqa: SLF001
    assert "sk-abcdefghijklmnopqrstuv" not in row["args_json"]
    assert "***" in row["args_json"]
    conflict = call.model_copy(update={"name": "search_code"})
    with pytest.raises(ValueError, match="conflict"):
        await store.record_tool_invocation(conflict, result)
    with pytest.raises(ValueError, match="task_id not found"):
        await store.record_tool_invocation(
            call.model_copy(update={"tool_call_id": "tc-2", "task_id": "missing"}),
            result.model_copy(update={"tool_call_id": "tc-2"}),
        )


@pytest.mark.asyncio
async def test_record_tool_invocation_caps_data_and_keeps_json(store: SqliteStorage):
    from reposage.domain.enums import ReviewTaskKind, ReviewTaskStatus, ToolCallStatus
    from reposage.domain.models import ToolCall, ToolResult
    from reposage.domain.run import ReviewTask
    from reposage.observability.tool_trace import AUDIT_DATA_MAX_CHARS

    await store.record_run(ReviewRun(run_id="run-t", head_sha="h"))
    await store.record_tasks(
        [
            ReviewTask(
                task_id="task-1",
                run_id="run-t",
                kind=ReviewTaskKind.FILE_REVIEW,
                target="src/a.py",
                status=ReviewTaskStatus.RUNNING,
            )
        ]
    )
    huge = "x" * 80_000
    call = ToolCall(
        tool_call_id="tc-data",
        name="read_file",
        arguments={"path": "src/a.py"},
        task_id="task-1",
        status=ToolCallStatus.OK,
    )
    result = ToolResult(tool_call_id="tc-data", data=huge, truncated=False)
    await store.record_tool_invocation(call, result)
    await store.record_tool_invocation(call, result)
    row = store._query(  # noqa: SLF001
        "SELECT data, truncated FROM tool_results WHERE tool_call_id='tc-data'"
    )[0]
    assert len(row["data"]) <= AUDIT_DATA_MAX_CHARS
    parsed = json.loads(row["data"])
    assert parsed["_audit_truncated"] is True
    assert row["truncated"] == 1
    other = result.model_copy(update={"data": "y" * 80_000})
    with pytest.raises(ValueError, match="conflict"):
        await store.record_tool_invocation(call, other)
