"""V2-E CLI mark/list/revoke。"""

from pathlib import Path

from reposage.app.cli import app
from reposage.domain.enums import FindingCategory, FindingStatus, Severity
from reposage.domain.finding import Finding
from reposage.domain.run import ReviewRun
from reposage.storage.sqlite import SqliteStorage
from typer.testing import CliRunner


def test_feedback_cli_mark_list_revoke_and_global_reject(tmp_path: Path):
    (tmp_path / "reposage.yaml").write_text("storage:\n  path: test.db\n", encoding="utf-8")
    runner = CliRunner()
    marked = runner.invoke(
        app,
        [
            "feedback",
            "mark",
            "--kind",
            "false_positive",
            "--repo",
            str(tmp_path),
            "--path",
            "src/a.py",
            "--category",
            "security",
            "--rationale",
            "secret-should-not-list",
        ],
    )
    assert marked.exit_code == 0, marked.output
    listed = runner.invoke(app, ["feedback", "list", "--repo", str(tmp_path)])
    assert listed.exit_code == 0
    assert "kind=false_positive" in listed.output
    assert "secret-should-not-list" not in listed.output
    verbose = runner.invoke(app, ["feedback", "list", "--repo", str(tmp_path), "--verbose"])
    assert "secret-should-not-list" in verbose.output
    rejected = runner.invoke(
        app, ["feedback", "mark", "--kind", "wont_fix", "--repo", str(tmp_path)]
    )
    assert rejected.exit_code != 0
    revoked = runner.invoke(app, ["feedback", "revoke", "--id", "1", "--repo", str(tmp_path)])
    assert revoked.exit_code == 0
    after = runner.invoke(app, ["feedback", "list", "--repo", str(tmp_path)])
    assert after.output.strip() == "0"
    again = runner.invoke(app, ["feedback", "revoke", "--id", "1", "--repo", str(tmp_path)])
    assert again.exit_code == 0
    missing = runner.invoke(app, ["feedback", "revoke", "--id", "999", "--repo", str(tmp_path)])
    assert missing.exit_code != 0
    assert "not found" in missing.output


def test_feedback_cli_revoke_rejects_other_repo(tmp_path: Path):
    import asyncio

    from reposage.domain.enums import FeedbackKind
    from reposage.domain.run import FeedbackMemory

    (tmp_path / "reposage.yaml").write_text("storage:\n  path: test.db\n", encoding="utf-8")
    store = SqliteStorage(tmp_path / "test.db")
    other_id = asyncio.run(
        store.record_feedback(
            FeedbackMemory(
                repo="OtherProject",
                kind=FeedbackKind.WONT_FIX,
                path="src/b.py",
                rationale="other-repo",
            )
        )
    )
    store.close()
    runner = CliRunner()
    revoked = runner.invoke(
        app, ["feedback", "revoke", "--id", str(other_id), "--repo", str(tmp_path)]
    )
    assert revoked.exit_code != 0
    assert "does not belong to this repo" in revoked.output
    store2 = SqliteStorage(tmp_path / "test.db")
    remaining = asyncio.run(store2.load_active_feedback("OtherProject"))
    store2.close()
    assert len(remaining) == 1
    assert remaining[0].id == other_id


def test_feedback_cli_from_finding_does_not_copy_cross_run(tmp_path: Path):
    (tmp_path / "reposage.yaml").write_text("storage:\n  path: test.db\n", encoding="utf-8")
    store = SqliteStorage(tmp_path / "test.db")
    import asyncio

    async def seed() -> None:
        await store.record_run(ReviewRun(run_id="run-1", head_sha="h"))
        await store.record_findings(
            [
                Finding(
                    finding_occurrence_id="occ-1",
                    run_id="run-1",
                    fingerprint="fp",
                    cross_run_match_key="line-anchored-key",
                    title="t",
                    severity=Severity.HIGH,
                    confidence=0.9,
                    category=FindingCategory.SECURITY,
                    canonical_path="src/a.py",
                    canonical_start_line=4,
                    status=FindingStatus.ACCEPTED,
                    rule_id="ruff:S110",
                )
            ]
        )

    asyncio.run(seed())
    store.close()
    runner = CliRunner()
    marked = runner.invoke(
        app,
        [
            "feedback",
            "mark",
            "--kind",
            "false_positive",
            "--repo",
            str(tmp_path),
            "--from-finding",
            "occ-1",
        ],
    )
    assert marked.exit_code == 0, marked.output
    store2 = SqliteStorage(tmp_path / "test.db")
    rows = asyncio.run(store2.list_feedback("RepoSage", include_revoked=False))
    store2.close()
    assert len(rows) == 1
    assert rows[0].path == "src/a.py"
    assert rows[0].category == "security"
    assert rows[0].cross_run_match_key is None
