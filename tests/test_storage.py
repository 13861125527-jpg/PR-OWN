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
from reposage.domain.run import ReviewRun
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


@pytest.mark.asyncio
async def test_record_and_read_run(store: SqliteStorage):
    run = ReviewRun(run_id="run-1", base_sha="a" * 7, head_sha="b" * 7, warnings=["w1", "w2"])
    await store.record_run(run)

    row = store._query("SELECT * FROM runs WHERE run_id='run-1'")[0]  # noqa: SLF001
    assert row["head_sha"] == "b" * 7
    # P1-4：warnings_json 只存 warnings 列表，不是整个 run
    assert json.loads(row["warnings_json"]) == ["w1", "w2"]


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
