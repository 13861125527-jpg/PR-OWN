"""ReviewService 端到端测试（V1-d，04 §1 骨架编排）。"""

import pytest
from reposage.config.settings import Settings
from reposage.domain.enums import (
    ChangeRequestSource,
    FindingCategory,
    FindingStatus,
    ReviewRunStatus,
    ReviewStrategyName,
    Severity,
    StageName,
)
from reposage.domain.finding import FindingCandidate
from reposage.providers.git.fake import FakeGitProvider
from reposage.providers.llm.fake import FakeLLMProvider
from reposage.review.service import ReviewService
from reposage.storage.sqlite import SqliteStorage


def _cand(path="src/app.py", start=3) -> FindingCandidate:
    return FindingCandidate(
        title="eval 动态执行",
        severity=Severity.HIGH,
        confidence=0.9,
        category=FindingCategory.SECURITY,
        claimed_path=path,
        claimed_start_line=start,
        trigger_condition="eval(data)",
        explanation="动态代码执行",
        impact="RCE",
        suggestion="白名单",
    )


def _fake_git() -> FakeGitProvider:
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/app.py": "import os\n\ndef handle(d):\n    return d\n"})
    fake.add_snapshot(
        "feat/x",
        {
            "src/app.py": (
                "import os\n\ndef handle(d):\n"
                "    return eval(d)\n"  # 新增行 4
                "    return os.system(d)\n"  # 新增行 5
            )
        },
    )
    fake.add_pr(1, base="main", head="feat/x", title="fix: auth", description="handle user input")
    return fake


def _make_service(fake_git, llm) -> ReviewService:
    settings = Settings()
    settings.review.languages = ["python"]
    return ReviewService(
        fake_git,
        llm,
        SqliteStorage(":memory:"),
        settings=settings,
    )


@pytest.mark.asyncio
async def test_review_end_to_end():
    fake = _fake_git()
    llm = FakeLLMProvider(default_findings=[_cand()])
    service = _make_service(fake, llm)

    run, findings = await service.review("1")

    # run 元数据
    assert run.status is ReviewRunStatus.COMPLETED
    assert run.external_ref == "1"
    assert run.base_sha == "main"
    assert run.head_sha == "feat/x"
    assert run.strategy is ReviewStrategyName.SINGLE_PASS
    stages = {s.stage for s in run.stages}
    assert StageName.PREFLIGHT in stages
    assert StageName.FETCH in stages
    assert StageName.CONTEXT in stages
    assert StageName.REVIEW in stages
    assert StageName.PIPELINE in stages

    # findings：命中新增行 → accepted + canonical 锚定
    assert len(findings) == 1
    f = findings[0]
    assert f.status is FindingStatus.ACCEPTED
    assert f.canonical_path == "src/app.py"
    assert f.canonical_start_line == 4
    assert f.fingerprint and f.cross_run_match_key and f.cluster_id


@pytest.mark.asyncio
async def test_review_storage_persisted():
    fake = _fake_git()
    llm = FakeLLMProvider(default_findings=[_cand()])
    store = SqliteStorage(":memory:")
    service = ReviewService(fake, llm, store)
    run, findings = await service.review("1")

    rows = store._conn.execute(
        "SELECT run_id, status FROM runs WHERE run_id = ?", (run.run_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == run.run_id


    conn = store._conn
    n_findings = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE run_id = ?", (run.run_id,)
    ).fetchone()[0]
    assert n_findings == len(findings)
    n_usage = conn.execute(
        "SELECT COUNT(*) FROM usages WHERE run_id = ?", (run.run_id,)
    ).fetchone()[0]
    assert n_usage >= 1


@pytest.mark.asyncio
async def test_review_fails_on_missing_head_lock():
    """head 未锁定 → 拒绝（SHA 锁定铁律）。"""
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/a.py": "x"})
    fake.add_snapshot("feat/x", {"src/a.py": "y"})
    fake.add_pr(1, base="main", head="feat/x", title="t")

    # 手动构造未锁定 ChangeRequest：fake 的 PR head 已 locked，这里模拟直接调 service
    # require_head_locked 在 get_changes 之后；FakeGitProvider 默认 head locked=True，
    # 因此构造一个不锁定 head 的 provider 场景：直接验证 require_head_locked
    from reposage.domain.models import ChangeRequest, CommitRef

    req = ChangeRequest(
        source=ChangeRequestSource.GITHUB_PR,
        external_id="1",
        base=CommitRef(sha="main", label="base"),
        head=CommitRef(sha="feat/x", label="head"),  # locked=False
        title="t",
    )
    with pytest.raises(ValueError, match="head 未锁定"):
        req.require_head_locked()


@pytest.mark.asyncio
async def test_review_empty_diff_no_findings():
    """无变更 → 无 findings，状态 COMPLETED。"""
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/a.py": "x"})
    fake.add_snapshot("feat/x", {"src/a.py": "x"})  # 无差异
    fake.add_pr(1, base="main", head="feat/x", title="t")
    service = _make_service(fake, FakeLLMProvider())
    run, findings = await service.review("1")
    assert run.status is ReviewRunStatus.COMPLETED
    assert findings == []
# ================= V1-d 返工：P2-1 阶段错误归因 =================


class _FailingLLM:
    """structured 抛异常 → 应归因 REVIEW 阶段。"""

    async def structured(self, messages, **kwargs):
        raise RuntimeError("llm boom")

    async def complete(self, *a, **k):
        raise AssertionError

    async def tool_loop(self, *a, **k):
        raise AssertionError


@pytest.mark.asyncio
async def test_stage_attribution_get_changes_failure_is_fetch():
    """P2-2：get_changes 失败 → 失败阶段归因 FETCH（不是 PREFLIGHT 重复失败）。"""

    class BoomGit:
        async def get_changes(self, ref):
            raise RuntimeError("git boom")

        async def get_diff(self, base_sha, head_sha, paths=None):
            raise AssertionError

        async def publish_comments(self, plan):
            raise AssertionError

    fake = BoomGit()
    store = SqliteStorage(":memory:")
    captured: dict = {}

    async def capture(run):
        captured["run"] = run

    service = ReviewService(fake, FakeLLMProvider(), store)
    orig = store.record_run
    store.record_run = capture  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="git boom"):
        await service.review("1")
    store.record_run = orig  # type: ignore[method-assign]
    run = captured["run"]
    assert run.status is ReviewRunStatus.FAILED
    failed = [s for s in run.stages if s.status == "failed"]
    assert failed, "应有 FAILED 阶段"
    assert failed[0].stage is StageName.FETCH  # 归因 FETCH，不是第二个 PREFLIGHT
    assert failed[0].error and "git boom" in failed[0].error
    preflight = [s for s in run.stages if s.stage is StageName.PREFLIGHT]
    assert len(preflight) == 1 and preflight[0].status == "ok"  # PREFLIGHT 只记一次 OK


@pytest.mark.asyncio
async def test_stage_attribution_llm_failure():
    """strategy 抛异常 → 失败阶段归因 REVIEW（不是 PREFLIGHT）。"""

    class BoomStrategy:
        name = "boom"

        def supports(self, run):
            return True

        async def execute(self, units, run, budget):
            raise RuntimeError("strategy boom")

    fake = _fake_git()
    store = SqliteStorage(":memory:")
    captured: dict = {}

    async def capture(run):
        captured["run"] = run

    service = _make_service(fake, FakeLLMProvider())
    service.storage = store
    service.strategy = BoomStrategy()  # type: ignore[assignment]
    orig = store.record_run
    store.record_run = capture  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await service.review("1")
    store.record_run = orig  # type: ignore[method-assign]
    run = captured["run"]
    assert run.status is ReviewRunStatus.FAILED
    failed = [s for s in run.stages if s.status == "failed"]
    assert failed, "应有 FAILED 阶段"
    assert failed[0].stage is StageName.REVIEW
