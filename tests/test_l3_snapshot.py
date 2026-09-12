"""L3 snapshot 准备：候选排序、cap、取消、IO 诊断。"""

from __future__ import annotations

import asyncio

import pytest
from reposage.domain.diff import parse_unified_diff
from reposage.providers.git.fake import FakeGitProvider
from reposage.review.symbols.snapshot import prepare_l3_snapshot, rank_candidate_paths


def test_rank_prefers_named_test_over_unrelated():
    unrelated = [f"tests/test_{i:03d}.py" for i in range(40)]
    ranked = rank_candidate_paths(
        path_set={*unrelated, "tests/test_helper.py", "src/util.py"},
        changed_paths={"src/app.py"},
        import_targets=["src/util.py"],
        names={"helper"},
    )
    assert ranked[0] == "src/util.py"
    assert "tests/test_helper.py" in ranked
    assert ranked.index("tests/test_helper.py") < 5
    assert unrelated[0] not in ranked


@pytest.mark.asyncio
async def test_prepare_reads_relevant_test_despite_unrelated_first():
    fake = FakeGitProvider()
    blobs = {f"tests/test_{i:03d}.py": "def test_noise():\n    return 1\n" for i in range(40)}
    blobs["src/app.py"] = "from src.util import helper\ndef run():\n    helper(1)\n"
    blobs["src/util.py"] = "def helper(x):\n    return x\n"
    blobs["tests/test_helper.py"] = "def test_helper():\n    assert helper(1) == 1\n"
    fake.add_snapshot("head", blobs)
    diff = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 from src.util import helper
 def run():
+    helper(1)
"""
    changed = parse_unified_diff(diff)
    changed[0].language = "python"
    prepared = await prepare_l3_snapshot(
        fake, "head", changed, repository_id="fake", max_files_read=5
    )
    assert "src/util.py" in prepared.snapshot.blobs
    assert "tests/test_helper.py" in prepared.snapshot.blobs
    assert "tests/test_000.py" not in prepared.snapshot.blobs


@pytest.mark.asyncio
async def test_prepare_propagates_cancelled_error():
    class Boom:
        repository_id = "x"

        async def list_paths(self, sha: str) -> list[str]:
            raise asyncio.CancelledError()

        async def get_blob(self, sha: str, path: str) -> str | None:
            return None

    with pytest.raises(asyncio.CancelledError):
        await prepare_l3_snapshot(Boom(), "head", [], repository_id="x")


@pytest.mark.asyncio
async def test_prepare_records_io_failure_not_cap():
    class Flaky(FakeGitProvider):
        async def get_blob(self, sha: str, path: str) -> str | None:
            if path == "src/util.py":
                raise RuntimeError("disk")
            return await super().get_blob(sha, path)

    fake = Flaky()
    fake.add_snapshot(
        "head",
        {
            "src/app.py": "from src.util import helper\ndef run():\n    helper(1)\n",
            "src/util.py": "def helper(x):\n    return x\n",
        },
    )
    diff = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 from src.util import helper
 def run():
+    helper(1)
"""
    changed = parse_unified_diff(diff)
    changed[0].language = "python"
    prepared = await prepare_l3_snapshot(fake, "head", changed, repository_id="fake")
    assert prepared.diagnostics.io_failures
    assert prepared.diagnostics.io_failures[0].path == "src/util.py"
    assert prepared.diagnostics.truncated_by_cap is False


@pytest.mark.asyncio
async def test_prepare_skips_unused_import_targets_under_cap():
    class Counting(FakeGitProvider):
        def __init__(self) -> None:
            super().__init__()
            self.requested: list[str] = []

        async def get_blob(self, sha: str, path: str) -> str | None:
            self.requested.append(path)
            return await super().get_blob(sha, path)

    fake = Counting()
    blobs = {
        "src/app.py": (
            "from src.unused_a import a\n"
            "from src.unused_b import b\n"
            "from src.util import helper\n"
            "def run():\n"
            "    helper(1)\n"
        ),
        "src/unused_a.py": "def a():\n    return 1\n",
        "src/unused_b.py": "def b():\n    return 1\n",
        "src/util.py": "def helper(x):\n    return x\n",
        "tests/test_helper.py": "def test_helper():\n    assert helper(1) == 1\n",
    }
    fake.add_snapshot("head", blobs)
    diff = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,4 +1,5 @@
 from src.unused_a import a
 from src.unused_b import b
 from src.util import helper
 def run():
+    helper(1)
"""
    changed = parse_unified_diff(diff)
    changed[0].language = "python"
    prepared = await prepare_l3_snapshot(
        fake, "head", changed, repository_id="fake", max_files_read=3
    )
    assert "src/util.py" in prepared.snapshot.blobs
    assert "src/unused_a.py" not in fake.requested
    assert "src/unused_b.py" not in fake.requested
    assert "src/util.py" in fake.requested


@pytest.mark.asyncio
async def test_cap_skip_marks_changed_file_owner():
    from reposage.domain.enums import CoverageReason
    from reposage.domain.models import ChangeRequest, ChangeRequestSource, CommitRef
    from reposage.review.context import ContextAssembler

    fake = FakeGitProvider()
    blobs = {
        "src/app.py": "from src.util import helper\ndef run():\n    helper(1)\n",
        "src/util.py": "def helper(x):\n    return x\n",
        "tests/test_helper.py": "def test_helper():\n    assert helper(1) == 1\n",
    }
    fake.add_snapshot("head", blobs)
    diff = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 from src.util import helper
 def run():
+    helper(1)
"""
    changed = parse_unified_diff(diff)
    changed[0].language = "python"
    prepared = await prepare_l3_snapshot(
        fake, "head", changed, repository_id="fake", max_files_read=1
    )
    assert "tests/test_helper.py" not in prepared.snapshot.blobs
    assert "src/app.py" in prepared.diagnostics.cap_affected_paths
    req = ChangeRequest(
        source=ChangeRequestSource.LOCAL_RANGE,
        base=CommitRef(sha="base", label="base"),
        head=CommitRef(sha="head", label="head", locked=True),
        title="t",
    )
    unit = ContextAssembler(symbol_retrieval=True).build_file_units(
        run_id="r", change_request=req, file=changed[0], snapshot=prepared.snapshot
    )[0]
    assert any(
        i.reason is CoverageReason.TRUNCATED and i.detail == "L3 candidate read cap"
        for i in unit.coverage.items
    )
