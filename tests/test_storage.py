"""SQLite 基础存储测试。"""

import sqlite3

import pytest
from reposage.domain.enums import FindingCategory, Severity
from reposage.domain.finding import Finding
from reposage.domain.models import ModelUsage, ModelUsageOutcome
from reposage.domain.run import ReviewRun
from reposage.storage.sqlite import SqliteStorage


@pytest.fixture
def store(tmp_path):
    s = SqliteStorage(tmp_path / "test.db")
    yield s
    s.close()


def test_schema_initialized(store: SqliteStorage):
    tables = {
        r[0]
        for r in store._conn.execute(  # noqa: SLF001
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for expected in (
        "runs",
        "findings",
        "finding_versions",
        "usages",
        "coverages",
        "tool_calls",
        "tool_results",
        "publish_plans",
        "published_comments",
        "feedback",
        "pr_caches",
    ):
        assert expected in tables


def test_record_and_read_run(store: SqliteStorage):
    run = ReviewRun(run_id="run-1", base_sha="a" * 7, head_sha="b" * 7)
    store.record_run(run)

    row = store._conn.execute("SELECT * FROM runs WHERE run_id='run-1'").fetchone()  # noqa: SLF001
    assert row is not None
    assert row["head_sha"] == "b" * 7


def test_record_finding(store: SqliteStorage):
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
    )
    store.record_findings([finding])

    row = store._conn.execute(  # noqa: SLF001
        "SELECT * FROM findings WHERE finding_occurrence_id='occ-1'"
    ).fetchone()
    assert row is not None
    assert row["canonical_path"] == "src/a.py"
    assert row["is_outside_diff"] == 0  # bool 落库为整数


def test_record_usage_late_cancelled(store: SqliteStorage):
    """P0-R2-3：迟到响应的 usage 仍记账。"""
    usage = ModelUsage(
        model="m",
        role="general",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.01,
        outcome=ModelUsageOutcome.LATE_CANCELLED,
    )
    store.record_usage("run-1", usage)

    row = store._conn.execute("SELECT * FROM usages").fetchone()  # noqa: SLF001
    assert row["outcome"] == "late_cancelled"
    assert row["cost_usd"] == 0.01


def test_duplicate_fingerprint_rejected(store: SqliteStorage):
    """UNIQUE(run_id, fingerprint)：同 run 同指纹第二次插入抛 IntegrityError。"""
    mk = lambda occ, fp: Finding(  # noqa: E731
        finding_occurrence_id=occ,
        run_id="run-1",
        fingerprint=fp,
        cross_run_match_key="k",
        title="t",
        severity=Severity.MEDIUM,
        confidence=0.8,
        category=FindingCategory.CORRECTNESS,
    )
    store.record_findings([mk("occ-1", "fp")])
    with pytest.raises(sqlite3.IntegrityError):
        store.record_findings([mk("occ-2", "fp")])
