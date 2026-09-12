"""V2-C 静态分析转换 / runner / ruff 集成。"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from reposage.config.settings import Settings
from reposage.domain.diff import parse_unified_diff
from reposage.domain.enums import (
    FindingCategory,
    FindingSourceKind,
    FindingStatus,
    ReviewRunStatus,
    ReviewTaskKind,
    Severity,
)
from reposage.domain.finding import FindingCandidate
from reposage.review.candidates import sanitize_llm_candidate
from reposage.review.static.convert import convert_diagnostics
from reposage.review.static.fake import FakeStaticAnalyzer
from reposage.review.static.protocol import AnalyzerDiagnostic
from reposage.review.static.ruff import RuffAnalyzer
from reposage.review.static.runner import run_static_analysis
from reposage.review.symbols.snapshot import HeadSnapshot

DIFF = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,4 @@
 def f():
     return 1
+def g(x=[]):
+    return x
"""


def _file_map():
    return {"src/app.py": parse_unified_diff(DIFF)[0]}


def _diag(**overrides: object) -> AnalyzerDiagnostic:
    data: dict[str, object] = {
        "analyzer_id": "ruff",
        "rule_id": "B006",
        "path": "src/app.py",
        "start_line": 3,
        "message": "Do not use mutable data structures for argument defaults",
    }
    data.update(overrides)
    return AnalyzerDiagnostic(
        analyzer_id=str(data["analyzer_id"]),
        rule_id=str(data["rule_id"]),
        path=str(data["path"]),
        start_line=int(data["start_line"]),
        message=str(data.get("message") or ""),
    )


def test_convert_valid_diagnostic():
    cands, skipped = convert_diagnostics([_diag()], _file_map())
    assert skipped == []
    assert len(cands) == 1
    cand = cands[0]
    assert cand.source_kind is FindingSourceKind.STATIC_ANALYZER
    assert cand.analyzer_id == "ruff"
    assert cand.rule_id == "ruff:B006"
    assert cand.role_id is None
    assert cand.category is FindingCategory.CORRECTNESS
    assert cand.severity is Severity.MEDIUM
    assert cand.confidence == 1.0
    assert cand.claimed_path == "src/app.py"
    assert cand.claimed_start_line == 3


def test_convert_rejects_traversal_and_unknown_and_old_line():
    fmap = _file_map()
    bad, skip1 = convert_diagnostics([_diag(path="../etc/passwd")], fmap)
    assert bad == []
    assert skip1
    unknown, skip2 = convert_diagnostics([_diag(path="src/missing.py")], fmap)
    assert unknown == []
    assert skip2
    old, skip3 = convert_diagnostics([_diag(start_line=1)], fmap)
    assert old == []
    assert skip3


def test_sanitize_llm_candidate_strips_program_fields():
    cand = FindingCandidate(
        title="t",
        severity=Severity.HIGH,
        confidence=0.9,
        category=FindingCategory.SECURITY,
        source_kind=FindingSourceKind.STATIC_ANALYZER,
        rule_id="ruff:B006",
        analyzer_id="ruff",
        role_id="security",
    )
    clean = sanitize_llm_candidate(cand)
    assert clean.source_kind is None
    assert clean.rule_id is None
    assert clean.analyzer_id is None
    assert clean.role_id == "security"


@pytest.mark.asyncio
async def test_runner_no_snapshot_is_capability_miss():
    settings = Settings().review.static
    settings.enabled = True
    result = await run_static_analysis(
        run_id="r1",
        files=list(_file_map().values()),
        file_map=_file_map(),
        head_sha="head",
        settings=settings,
        provider=None,
        snapshot=None,
        analyzer=FakeStaticAnalyzer(hang=True),
    )
    assert result.status == "failed"
    assert any("capability miss" in w for w in result.warnings)
    assert any(t.kind is ReviewTaskKind.STATIC_ANALYZE for t in result.tasks)


@pytest.mark.asyncio
async def test_runner_cancel_fake_hang():
    settings = Settings().review.static
    settings.enabled = True
    settings.timeout_seconds = 30
    snap = HeadSnapshot.from_blobs("head", {"src/app.py": "def g(x=[]):\n    return x\n"})
    task = asyncio.create_task(
        run_static_analysis(
            run_id="r1",
            files=list(_file_map().values()),
            file_map=_file_map(),
            head_sha="head",
            settings=settings,
            provider=None,
            snapshot=snap,
            analyzer=FakeStaticAnalyzer(hang=True),
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _ruff_available() -> bool:
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


@pytest.mark.asyncio
async def test_ruff_analyzer_detects_b006():
    if not _ruff_available():
        pytest.skip("python -m ruff 不可用")
    files = list(_file_map().values())
    blobs = {"src/app.py": "def f():\n    return 1\ndef g(x=[]):\n    return x\n"}
    raw = await RuffAnalyzer().analyze(
        files, blobs, timeout_s=30.0, select=["B", "S"], denylist=[]
    )
    assert raw.status == "ok"
    assert any(d.rule_id == "B006" and d.path == "src/app.py" for d in raw.diagnostics)


@pytest.mark.asyncio
async def test_ruff_exit_1_empty_stdout_is_capability_miss(monkeypatch):
    """B1：python -m ruff 缺模块时 exit 1 + 空 stdout，不得当成零诊断。"""

    class _FakeProc:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"No module named ruff"

        def kill(self) -> None:
            return None

    async def _fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        return _FakeProc()

    monkeypatch.setattr(
        "reposage.review.static.ruff.asyncio.create_subprocess_exec", _fake_exec
    )
    files = list(_file_map().values())
    blobs = {"src/app.py": "def g(x=[]):\n    return x\n"}
    raw = await RuffAnalyzer().analyze(
        files, blobs, timeout_s=30.0, select=["B", "S"], denylist=[]
    )
    assert raw.status == "failed"
    assert raw.diagnostics == []
    assert any("capability miss: ruff" in w for w in raw.warnings)


@pytest.mark.asyncio
async def test_service_static_off_has_disabled_status():
    from reposage.providers.git.fake import FakeGitProvider
    from reposage.providers.llm.fake import FakeLLMProvider
    from reposage.review.service import ReviewService
    from reposage.storage.sqlite import SqliteStorage

    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/app.py": "def handle(d):\n    return d\n"})
    fake.add_snapshot("feat/x", {"src/app.py": "def handle(d):\n    return eval(d)\n"})
    fake.add_pr(1, base="main", head="feat/x")
    settings = Settings()
    settings.review.static.enabled = False
    service = ReviewService(
        fake, FakeLLMProvider(), SqliteStorage(":memory:"), settings=settings
    )
    run, _ = await service.review("1")
    assert run.status is ReviewRunStatus.COMPLETED
    rows = service.storage._query(  # noqa: SLF001
        "SELECT kind FROM tasks WHERE run_id = ?", (run.run_id,)
    )
    assert all(r[0] != ReviewTaskKind.STATIC_ANALYZE.value for r in rows)
    review = next(s for s in run.stages if s.stage.value == "review")
    assert review.detail is not None
    assert '"static_status": "disabled"' in review.detail


@pytest.mark.asyncio
async def test_service_static_capability_miss():
    from reposage.providers.git.fake import FakeGitProvider
    from reposage.providers.llm.fake import FakeLLMProvider
    from reposage.review.service import ReviewService
    from reposage.storage.sqlite import SqliteStorage

    inner = FakeGitProvider()
    inner.add_snapshot("main", {"src/app.py": "def handle(d):\n    return d\n"})
    inner.add_snapshot("feat/x", {"src/app.py": "def handle(d):\n    return eval(d)\n"})
    inner.add_pr(1, base="main", head="feat/x")

    class NoSnap:
        async def get_changes(self, ref: str):
            return await inner.get_changes(ref)

        async def get_diff(self, base_sha: str, head_sha: str, paths: list[str] | None = None):
            return await inner.get_diff(base_sha, head_sha, paths)

        async def publish_comments(self, plan):
            return await inner.publish_comments(plan)

        async def delete_comment(self, request):
            return False

    settings = Settings()
    settings.review.languages = ["python"]
    settings.review.static.enabled = True
    settings.context.symbol_retrieval = False
    service = ReviewService(
        NoSnap(),  # type: ignore[arg-type]
        FakeLLMProvider(),
        SqliteStorage(":memory:"),
        settings=settings,
        snapshot_provider=None,
        static_analyzer=FakeStaticAnalyzer(diagnostics=[_diag(path="src/app.py", start_line=2)]),
    )
    run, _ = await service.review("1")
    assert run.status is ReviewRunStatus.COMPLETED
    assert any("capability miss" in w for w in run.warnings)
    assert run.coverage is not None
    assert run.coverage.truncated is True


@pytest.mark.asyncio
async def test_service_static_fuses_with_llm():
    from reposage.providers.git.fake import FakeGitProvider
    from reposage.providers.llm.fake import FakeLLMProvider
    from reposage.review.service import ReviewService
    from reposage.storage.sqlite import SqliteStorage

    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/app.py": "def handle(d):\n    return d\n"})
    fake.add_snapshot("feat/x", {"src/app.py": "def handle(d):\n    return eval(d)\n"})
    fake.add_pr(1, base="main", head="feat/x")
    llm_cand = FindingCandidate(
        title="eval 动态执行",
        severity=Severity.HIGH,
        confidence=1.0,
        category=FindingCategory.SECURITY,
        claimed_path="src/app.py",
        claimed_start_line=2,
        trigger_condition="eval(d)",
        explanation="dynamic",
    )
    settings = Settings()
    settings.review.languages = ["python"]
    settings.review.static.enabled = True
    settings.context.symbol_retrieval = False
    diag = AnalyzerDiagnostic(
        analyzer_id="ruff",
        rule_id="S307",
        path="src/app.py",
        start_line=2,
        message="eval",
    )
    store = SqliteStorage(":memory:")
    service = ReviewService(
        fake,
        FakeLLMProvider(default_findings=[llm_cand]),
        store,
        settings=settings,
        static_analyzer=FakeStaticAnalyzer(diagnostics=[diag]),
    )
    run, findings = await service.review("1")
    assert run.status is ReviewRunStatus.COMPLETED
    accepted = [f for f in findings if f.status is FindingStatus.ACCEPTED]
    assert len(accepted) == 1
    kinds = {s.kind for s in accepted[0].sources}
    assert FindingSourceKind.LLM_GENERAL in kinds
    assert FindingSourceKind.STATIC_ANALYZER in kinds
    rows = store._query("SELECT kind FROM tasks WHERE run_id = ?", (run.run_id,))  # noqa: SLF001
    assert any(r[0] == ReviewTaskKind.STATIC_ANALYZE.value for r in rows)
