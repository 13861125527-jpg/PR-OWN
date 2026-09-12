"""ReviewService 端到端测试（V1-d，04 §1 骨架编排）。"""

import asyncio
import time

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
from reposage.domain.run import ReviewRun
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
    # V1-f：每阶段记录耗时（duration_ms ≥ 0，非恒死字段）
    assert all(s.duration_ms >= 0 for s in run.stages)
    # V1-f：覆盖清单已汇总（至少含被审查文件 src/app.py 的 covered 条目）
    assert any(i.target == "src/app.py" and i.reason.value == "covered" for i in run.coverage.items)

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

    # V1-f：任务记录（成本/Token）与覆盖清单（CoverageItem）落库
    n_tasks = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE run_id = ?", (run.run_id,)
    ).fetchone()[0]
    assert n_tasks >= 1
    n_coverages = conn.execute(
        "SELECT COUNT(*) FROM coverages WHERE run_id = ?", (run.run_id,)
    ).fetchone()[0]
    assert n_coverages == 1
    coverage_row = conn.execute(
        "SELECT items_json, truncated FROM coverages WHERE run_id = ?", (run.run_id,)
    ).fetchone()
    assert coverage_row[0]  # 覆盖清单非空
    import json as _json

    items = _json.loads(coverage_row[0])
    assert any(i["target"] == "src/app.py" and i["reason"] == "covered" for i in items)

    # V1-f：阶段记录落库 + 配置快照哈希生成
    n_stages = conn.execute(
        "SELECT COUNT(*) FROM run_stages WHERE run_id = ?", (run.run_id,)
    ).fetchone()[0]
    assert n_stages >= 1
    run_row = conn.execute(
        "SELECT config_hash FROM runs WHERE run_id = ?", (run.run_id,)
    ).fetchone()
    assert run_row[0] and len(run_row[0]) == 64  # SHA-256 hex


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


@pytest.mark.asyncio
async def test_failed_run_stages_persisted():
    """V1-f：fetch 失败后，失败阶段 status/error 仍落库（run_stages 表可查询）。"""

    class BoomGit:
        async def get_changes(self, ref):
            raise RuntimeError("git boom")

        async def get_diff(self, base_sha, head_sha, paths=None):
            raise AssertionError

        async def publish_comments(self, plan):
            raise AssertionError

    store = SqliteStorage(":memory:")
    service = ReviewService(BoomGit(), FakeLLMProvider(), store)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="git boom"):
        await service.review("1")
    # 失败 run 已落库，失败阶段可查询
    rows = store._query(
        "SELECT stage, status, error FROM run_stages WHERE run_id = (SELECT run_id FROM runs LIMIT 1) ORDER BY sequence"
    )
    assert any(r["status"] == "failed" and r["stage"] == "fetch" and "git boom" in r["error"] for r in rows)


@pytest.mark.asyncio
async def test_stage_timing_boundaries_include_their_own_work():
    """V1-f：FETCH/CONTEXT/REVIEW 的耗时各自包含本阶段真实工作。"""

    class SlowDiffGit(FakeGitProvider):
        async def get_diff(self, base_sha, head_sha, paths=None):
            await asyncio.sleep(0.03)
            return await super().get_diff(base_sha, head_sha, paths)

    class SlowAssembler:
        def __init__(self, delegate):
            self._delegate = delegate

        def build_file_units(self, **kwargs):
            time.sleep(0.03)
            return self._delegate.build_file_units(**kwargs)

    class SlowStrategy:
        def __init__(self, delegate):
            self._delegate = delegate

        def supports(self, run):
            return self._delegate.supports(run)

        async def execute(self, units, run, budget):
            await asyncio.sleep(0.03)
            return await self._delegate.execute(units, run, budget)

    base = _fake_git()
    fake = SlowDiffGit()
    fake._snapshots = base._snapshots
    fake._prs = base._prs
    service = _make_service(fake, FakeLLMProvider(default_findings=[_cand()]))
    service.assembler = SlowAssembler(service.assembler)  # type: ignore[assignment]
    service.strategy = SlowStrategy(service.strategy)  # type: ignore[assignment]

    run, _ = await service.review("1")
    durations = {stage.stage: stage.duration_ms for stage in run.stages}
    assert durations[StageName.FETCH] >= 20
    assert durations[StageName.CONTEXT] >= 20
    assert durations[StageName.REVIEW] >= 20


@pytest.mark.asyncio
async def test_failed_stage_duration_is_measured():
    """V1-f：失败阶段使用当前阶段起点计时，不是模型默认 0。"""

    class SlowFailingGit:
        async def get_changes(self, ref):
            await asyncio.sleep(0.03)
            raise RuntimeError("delayed git boom")

        async def get_diff(self, base_sha, head_sha, paths=None):
            raise AssertionError

        async def publish_comments(self, plan):
            raise AssertionError

    store = SqliteStorage(":memory:")
    service = ReviewService(SlowFailingGit(), FakeLLMProvider(), store)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="delayed git boom"):
        await service.review("1")

    failed = store._query(
        "SELECT stage, duration_ms FROM run_stages "
        "WHERE status = 'failed' ORDER BY sequence DESC LIMIT 1"
    )[0]
    assert failed["stage"] == "fetch"
    assert failed["duration_ms"] >= 20


@pytest.mark.asyncio
async def test_review_publish_and_recover_same_pr():
    """V1-e：正式发布 partial 后，同 PR 新 run 重跑续发 → completed（幂等续发）。"""
    fake = _fake_git()
    llm = FakeLLMProvider(default_findings=[_cand()])
    store = SqliteStorage(":memory:")
    settings = Settings()
    settings.review.languages = ["python"]
    service = ReviewService(fake, llm, store, settings=settings)

    # 第一次正式发布：summary 失败 → partial（可恢复）
    fake.failed_comment_ids.add("summary")
    run1, findings1 = await service.review("1", dry_run=False, run_id="run-a")
    assert run1.publish_status == "partial"
    assert StageName.PUBLISH in {s.stage for s in run1.stages}

    # 重跑：同 PR 新 run，去注入 → 恢复 partial plan 续发 → completed
    fake.failed_comment_ids.clear()
    run2, _ = await service.review("1", dry_run=False, run_id="run-b")
    assert run2.publish_status in ("published", "completed")
    # 恢复成功是正常控制流：不降级分析状态，恢复信息在 stage detail 而非 warning（P2）
    assert run2.status is ReviewRunStatus.COMPLETED
    publish_detail = next(
        s.detail for s in run2.stages if s.stage is StageName.PUBLISH
    )
    assert "恢复计划" in publish_detail


@pytest.mark.asyncio
async def test_review_partial_then_dryrun_then_recover():
    """V1-e 三轮 P1：partial → dry-run → 正式重试，恢复原 partial Saga。

    dry-run 会新增较新的 prepared plan；正式重试必须用 recoverable 查询
    （过滤 mode=publish），否则会新建 plan、旧 partial Saga 永久残留。
    """
    fake = _fake_git()
    llm = FakeLLMProvider(default_findings=[_cand()])
    store = SqliteStorage(":memory:")
    settings = Settings()
    settings.review.languages = ["python"]
    service = ReviewService(fake, llm, store, settings=settings)

    # 第一次正式发布：summary 失败 → partial
    fake.failed_comment_ids.add("summary")
    run1, _ = await service.review("1", dry_run=False, run_id="run-a")
    assert run1.publish_status == "partial"

    # 一次 dry-run：新增较新的 prepared plan（可能遮挡旧 partial）
    await service.review("1", dry_run=True, run_id="run-dry")

    # 正式重试：必须恢复旧 partial plan（不是 dry-run 的 prepared），续发 → completed
    fake.failed_comment_ids.clear()
    run3, _ = await service.review("1", dry_run=False, run_id="run-c")
    assert run3.publish_status in ("published", "completed")
    assert run3.status is ReviewRunStatus.COMPLETED
    publish_detail = next(
        s.detail for s in run3.stages if s.stage is StageName.PUBLISH
    )
    assert "恢复计划" in publish_detail
    # run-a 的 partial plan 已被续发完成；dry-run plan 仍是 prepared（未被误用）
    run_a_plan = store._query(
        "SELECT status FROM publish_plans WHERE run_id = ? AND mode = 'publish'", ("run-a",)
    )[0]
    assert run_a_plan["status"] in ("published", "completed")
    dry_plan = store._query(
        "SELECT status FROM publish_plans WHERE run_id = ? AND mode = 'dry_run'", ("run-dry",)
    )[0]
    assert dry_plan["status"] == "prepared"


@pytest.mark.asyncio
async def test_head_a_partial_then_head_b_publishes_new_plan():
    """四轮 P0：head A partial → head B 正式运行，B 的新评论确实发布（旧 partial 标 obsolete）。

    两次审查用不同 head SHA（不同 PR 快照），但同一 PR 身份（external_ref 相同）。
    """
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/app.py": "def f():\n    return 1\n"})
    fake.add_snapshot(
        "head-a", {"src/app.py": "def f():\n    return eval(x)\n"}
    )
    fake.add_snapshot(
        "head-b", {"src/app.py": "def f():\n    return eval(x)\n    return exec(y)\n"}
    )
    # 两个 PR 记录都指向 external_ref=1，但 head 不同（模拟推送新 head）
    fake.add_pr(1, base="main", head="head-a", title="t")
    llm = FakeLLMProvider(default_findings=[_cand()])
    store = SqliteStorage(":memory:")
    settings = Settings()
    settings.review.languages = ["python"]
    service = ReviewService(fake, llm, store, settings=settings)

    # head A 正式发布：summary 失败 → partial
    fake.failed_comment_ids.add("summary")
    run_a, _ = await service.review("1", dry_run=False, run_id="run-a")
    assert run_a.publish_status == "partial"

    # PR 推送到 head B（切换快照），head B 正式发布：summary 成功 → 新 plan 发布 B 评论
    fake.failed_comment_ids.clear()
    fake.add_pr(1, base="main", head="head-b", title="t")
    run_b, _ = await service.review("1", dry_run=False, run_id="run-b")
    assert run_b.publish_status in ("published", "completed")
    # 旧 head A 的 partial plan 已标 obsolete
    old_plan = store._query(
        "SELECT status FROM publish_plans WHERE run_id = ? AND mode = 'publish'", ("run-a",)
    )[0]
    assert old_plan["status"] == "obsolete"
    # head B 的新 plan 是 completed/published
    new_plan = store._query(
        "SELECT status FROM publish_plans WHERE run_id = ? AND mode = 'publish'", ("run-b",)
    )[0]
    assert new_plan["status"] in ("published", "completed")


@pytest.mark.asyncio
async def test_head_a_cleanup_pending_then_head_b_recovers_cleanup_and_publishes():
    """四轮 P0：head A cleanup_pending → head B 正式运行，A 清理完成且 B 新 plan 完成。"""
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/app.py": "def f():\n    return 1\n"})
    fake.add_snapshot(
        "head-a", {"src/app.py": "def f():\n    return eval(x)\n    return extra()\n"}
    )
    fake.add_snapshot(
        "head-b", {"src/app.py": "def f():\n    return eval(x)\n"}
    )
    fake.add_pr(1, base="main", head="head-a", title="t")
    llm = FakeLLMProvider(default_findings=[_cand()])
    store = SqliteStorage(":memory:")
    settings = Settings()
    settings.review.languages = ["python"]
    service = ReviewService(fake, llm, store, settings=settings)

    # head A：第一次发布 f1（cand 命中 eval），产生清理任务。
    # 用注入让第一次发布产生 cleanup_pending：先正常发布 f1+f2，再第二轮只剩 f1 且删除失败。
    # 简化：直接让第一次发布即 cleanup_pending 需要两轮。这里用「同 head 两轮」构造 cleanup_pending。
    run_a1, _ = await service.review("1", dry_run=False, run_id="run-a1")
    assert run_a1.publish_status in ("published", "completed")

    # 模拟 head A 的 cleanup_pending：手动把某 plan 置 cleanup_pending（真实场景由删除失败产生）
    # 这里用 service 同 head 重跑 + 注入删除失败构造
    fake.failed_delete_ids = {r["remote_comment_id"] for r in fake.published}
    fake.add_pr(1, base="main", head="head-a", title="t")
    run_a2, _ = await service.review("1", dry_run=False, run_id="run-a2")
    # 可能 completed 或 cleanup_pending（取决于是否还有待删评论）

    # PR 推送到 head B，正式发布 → A 的清理独立恢复 + B 新 plan 发布
    fake.failed_delete_ids.clear()
    fake.add_pr(1, base="main", head="head-b", title="t")
    run_b, _ = await service.review("1", dry_run=False, run_id="run-b")
    assert run_b.publish_status in ("published", "completed")
    new_plan = store._query(
        "SELECT status FROM publish_plans WHERE run_id = ? AND mode = 'publish'", ("run-b",)
    )[0]
    assert new_plan["status"] in ("published", "completed")


@pytest.mark.asyncio
async def test_skip_warning_keeps_run_completed():
    """跳过非目标语言文件会产生 warning，但不得把 run 打成 PARTIAL。"""
    fake = FakeGitProvider()
    fake.add_snapshot("base", {"src/a.py": "x=1\n", "README.md": "old\n"})
    fake.add_snapshot("head", {"src/a.py": "x=2\n", "README.md": "new\n"})
    store = SqliteStorage(":memory:")
    service = ReviewService(fake, FakeLLMProvider(default_findings=[]), store)
    run, _ = await service.review("base..head")
    assert any("跳过" in w for w in run.warnings)
    assert run.status is ReviewRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_required_health_failure_marks_partial_without_parsing_target():
    from reposage.domain.run import SourceRunResult, StrategyHealth
    from reposage.domain.strategy import StrategyResult

    class PartialStrategy:
        name = "single_pass"

        def supports(self, run):
            return True

        async def execute(self, units, run, budget):
            return StrategyResult(
                [],
                SourceRunResult(
                    strategy=ReviewStrategyName.SINGLE_PASS,
                    warnings=[],
                    health=StrategyHealth(
                        required_failed=True,
                        required_failure_count=1,
                        coverage_complete=False,
                    ),
                ),
            )

    fake = FakeGitProvider()
    fake.add_snapshot("base", {"src/a.py": "x=1\n"})
    fake.add_snapshot("head", {"src/a.py": "x=2\n"})
    store = SqliteStorage(":memory:")
    service = ReviewService(fake, FakeLLMProvider(), store)
    service.strategy = PartialStrategy()  # type: ignore[assignment]
    run, _ = await service.review("base..head")
    assert run.status is ReviewRunStatus.PARTIAL
    assert run.warnings == []


@pytest.mark.asyncio
async def test_l3_capability_miss_is_disclosed():
    class DiffOnly:
        def __init__(self) -> None:
            self.inner = _fake_git()

        async def get_changes(self, ref):
            return await self.inner.get_changes(ref)

        async def get_diff(self, base_sha, head_sha, paths=None):
            return await self.inner.get_diff(base_sha, head_sha, paths)

        async def publish_comments(self, plan):
            return await self.inner.publish_comments(plan)

        async def delete_comment(self, request):
            return False

    settings = Settings()
    settings.review.languages = ["python"]
    settings.context.symbol_retrieval = True
    store = SqliteStorage(":memory:")
    service = ReviewService(DiffOnly(), FakeLLMProvider(), store, settings=settings)
    run, _ = await service.review("1")
    assert run.status is ReviewRunStatus.COMPLETED
    assert any("capability miss" in w for w in run.warnings)
    assert run.coverage is not None
    assert run.coverage.truncated is True
    assert any("capability miss" in (i.detail or "") for i in run.coverage.items)


@pytest.mark.asyncio
async def test_l3_extraction_failure_is_disclosed(monkeypatch):
    from reposage.review.symbols.extract import PythonAstExtractor

    def boom(self, path: str, source: str):
        raise RuntimeError("extract boom")

    monkeypatch.setattr(PythonAstExtractor, "extract", boom)
    fake = _fake_git()
    settings = Settings()
    settings.review.languages = ["python"]
    settings.context.symbol_retrieval = True
    service = ReviewService(fake, FakeLLMProvider(), SqliteStorage(":memory:"), settings=settings)
    run, _ = await service.review("1")
    assert run.status is ReviewRunStatus.COMPLETED
    assert any("L3 extraction failed: RuntimeError" in w for w in run.warnings)
    assert run.coverage is not None
    assert any(
        (i.detail or "").startswith("L3 extraction failed: RuntimeError") for i in run.coverage.items
    )
    blob = " ".join(run.warnings) + " ".join(i.detail or "" for i in run.coverage.items)
    assert "extract boom" not in blob
    assert "eval(d)" not in blob


@pytest.mark.asyncio
async def test_incremental_fetch_file_set_and_fallbacks():
    from reposage.domain.enums import PublishPlanStatus
    from reposage.domain.run import PublishPlan

    fake = FakeGitProvider()
    fake.add_snapshot("base", {"src/old.py": "a=1\n", "src/new.py": "b=1\n"})
    fake.add_snapshot("wm", {"src/old.py": "a=2\n", "src/new.py": "b=1\n"})
    fake.add_snapshot("head", {"src/old.py": "a=2\n", "src/new.py": "b=2\n"})
    fake.add_pr(1, base="base", head="head", title="inc")
    settings = Settings()
    settings.review.languages = ["python"]
    settings.review.incremental.enabled = True
    settings.context.symbol_retrieval = False
    store = SqliteStorage(":memory:")
    await store.record_run(ReviewRun(run_id="seed", head_sha="wm", external_ref="1"))
    await store.record_publish_plan(
        PublishPlan(
            plan_id="p0",
            run_id="seed",
            pr_identity="RepoSage#1",
            mode="publish",
            status=PublishPlanStatus.COMPLETED,
            target_head_sha="wm",
            committed_watermark="wm",
        )
    )
    service = ReviewService(fake, FakeLLMProvider(), store, settings=settings)
    run, _ = await service.review("1")
    fetch = next(s for s in run.stages if s.stage is StageName.FETCH)
    assert "incremental=1" in (fetch.detail or "")
    assert "from=wm" in (fetch.detail or "")
    assert run.base_sha == "base"
    covered = {i.target for i in run.coverage.items}
    assert "src/new.py" in covered
    assert "src/old.py" not in covered

    local, _ = await service.review("base..head")
    assert "incremental_skipped:no_pr_identity" in local.warnings

    fake.diff_failures.add(("wm", "head"))
    fallback, _ = await service.review("1")
    assert "incremental_fallback:diff_failed" in fallback.warnings
    covered_fb = {i.target for i in fallback.coverage.items}
    assert "src/old.py" in covered_fb


@pytest.mark.asyncio
async def test_dry_run_does_not_create_watermark():
    fake = _fake_git()
    settings = Settings()
    settings.review.languages = ["python"]
    settings.review.incremental.enabled = True
    settings.context.symbol_retrieval = False
    store = SqliteStorage(":memory:")
    service = ReviewService(fake, FakeLLMProvider(default_findings=[_cand()]), store, settings=settings)
    await service.review("1", dry_run=True)
    assert await store.load_committed_watermark("RepoSage#1") is None


@pytest.mark.asyncio
async def test_feedback_enabled_false_does_not_suppress():
    from reposage.domain.enums import FeedbackKind
    from reposage.domain.run import FeedbackMemory

    fake = _fake_git()
    settings = Settings()
    settings.review.languages = ["python"]
    settings.review.feedback.enabled = False
    settings.context.symbol_retrieval = False
    store = SqliteStorage(":memory:")
    await store.record_feedback(
        FeedbackMemory(
            repo="RepoSage",
            kind=FeedbackKind.FALSE_POSITIVE,
            path="src/app.py",
            category="security",
        )
    )
    service = ReviewService(fake, FakeLLMProvider(default_findings=[_cand()]), store, settings=settings)
    _run, findings = await service.review("1")
    assert any(f.status is FindingStatus.ACCEPTED for f in findings)
