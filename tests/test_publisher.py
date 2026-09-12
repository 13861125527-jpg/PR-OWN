"""Publisher（V1-e Publishing/Saga）测试，07 §7 / 10 §7 / 16 P0-R2-2。

覆盖 DoD：幂等零增量、部分失败重跑恢复、cleanup_pending、supersede 清理、
marker 复用、summary 作为必要评论。
"""

import json

import pytest
from reposage.domain.enums import (
    CommentKind,
    FindingStatus,
    PublishPlanStatus,
    Severity,
)
from reposage.domain.finding import Finding
from reposage.domain.run import ReviewRun
from reposage.providers.git.fake import FakeGitProvider
from reposage.publishing.publisher import Publisher
from reposage.storage.sqlite import SqliteStorage


def _finding(
    occurrence_id: str,
    *,
    status: FindingStatus = FindingStatus.ACCEPTED,
    severity: Severity = Severity.HIGH,
    path: str = "src/a.py",
    line: int = 3,
) -> Finding:
    return Finding(
        finding_occurrence_id=occurrence_id,
        run_id="run-1",
        fingerprint=f"fp-{occurrence_id}",
        cross_run_match_key=f"key-{occurrence_id}",
        title="eval 动态执行",
        severity=severity,
        confidence=0.9,
        category="security",
        canonical_path=path if status is not FindingStatus.BODY_ONLY else None,
        canonical_start_line=line if status is not FindingStatus.BODY_ONLY else None,
        explanation="动态代码执行",
        suggestion="白名单",
        status=status,
    )


def _run(run_id: str = "run-1", external_ref: str = "1") -> ReviewRun:
    return ReviewRun(run_id=run_id, head_sha="abc1234", external_ref=external_ref)


def _publisher(fake_git) -> tuple[Publisher, SqliteStorage]:
    store = SqliteStorage(":memory:")
    return Publisher(fake_git, store, repo="RepoSage"), store


def _seed(store: SqliteStorage, findings: list[Finding]) -> None:
    """落库 run + findings（publish 前必须，published_comments 有外键）。"""
    store._record_run(_run())
    store._record_findings(findings)


def _publisher_with_findings(fake_git, findings: list[Finding]) -> tuple[Publisher, SqliteStorage]:
    pub, store = _publisher(fake_git)
    _seed(store, findings)
    return pub, store


def test_build_plan_classification():
    """build_plan：summary 为第一条必要评论；accepted→inline；body_only→body；低严重度仅摘要。"""
    fake = FakeGitProvider()
    pub, _ = _publisher(fake)
    findings = [
        _finding("f1", severity=Severity.HIGH, path="src/a.py", line=3),  # inline
        _finding("f2", status=FindingStatus.BODY_ONLY),  # body
        _finding("f3", severity=Severity.LOW, path="src/a.py", line=4),  # 仅摘要
    ]
    plan = pub.build_plan(_run(), findings, mode="dry_run")
    # summary 第一条，kind=SUMMARY，required
    assert plan.comments[0].kind is CommentKind.SUMMARY
    assert plan.comments[0].required is True
    assert plan.comments[0].marker
    # 1 inline + 1 body（低严重度不进评论流）
    kinds = [c.kind for c in plan.comments[1:]]
    assert kinds == [CommentKind.INLINE, CommentKind.BODY]
    assert all(c.required for c in plan.comments)
    # marker 唯一，且含稳定 PR 身份（不含随机 plan_id，V1-e 返工 P0）
    assert len({c.marker for c in plan.comments}) == len(plan.comments)
    assert all("RepoSage#1" in c.marker for c in plan.comments)
    assert all(plan.plan_id not in c.marker for c in plan.comments)


def test_build_plan_stable_marker_across_runs():
    """V1-e 返工 P0：新 run/新 plan 生成的 marker 与旧 run 相同（稳定身份）。"""
    fake = FakeGitProvider()
    pub, _ = _publisher(fake)
    f1 = _finding("f1", path="src/a.py", line=3)
    plan1 = pub.build_plan(_run(run_id="run-a"), [f1], mode="publish")
    plan2 = pub.build_plan(_run(run_id="run-b"), [f1], mode="publish")
    assert plan1.plan_id != plan2.plan_id  # 不同 plan
    # 同 PR 身份 + 同 cross_run_match_key → marker 稳定相同
    m1 = {c.marker for c in plan1.comments}
    m2 = {c.marker for c in plan2.comments}
    assert m1 == m2


@pytest.mark.asyncio
async def test_publish_idempotent_zero_delta():
    """幂等（DoD，V1-e 返工 P0）：同 PR 新 run/新 plan 二次发布零增量（跨 run 稳定 marker）。"""
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])
    plan1 = pub.build_plan(_run(), [f1], mode="publish")  # run-1 已 seed
    await pub.publish(plan1)
    assert plan1.status is PublishPlanStatus.COMPLETED
    first_ids = {c.marker: c.remote_comment_id for c in plan1.comments}
    # 新 run + 新 plan（同一 PR，稳定 marker）→ 零增量（复用 remote_comment_id，非新建）
    store._record_run(_run(run_id="run-b"))
    plan2 = pub.build_plan(_run(run_id="run-b"), [f1], mode="publish")
    await pub.publish(plan2)
    assert plan2.status in (PublishPlanStatus.PUBLISHED, PublishPlanStatus.COMPLETED)
    for c in plan2.comments:
        assert c.remote_comment_id == first_ids[c.marker]  # 复用，非新建
    # DB 可加载且状态持久
    loaded = await store.load_publish_plan(plan2.plan_id)
    assert loaded is not None


@pytest.mark.asyncio
async def test_idempotent_across_provider_restart():
    """V1-e 返工 P0：模拟 Provider/进程重启（新 Fake 实例）仍零增量。

    真实语义：Provider 重启不影响远端（GitHub 评论仍在），仅内存清空；
    DB remote_comment_id 映射兜底 → 新 Fake 预填历史 remote_id → update-or-create
    复用（更新 body 保持 ID），不新增评论。
    """
    fake1 = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub1, store = _publisher_with_findings(fake1, [f1])
    plan1 = pub1.build_plan(_run(), [f1], mode="publish")  # run-1 已 seed
    await pub1.publish(plan1)
    n_published = len(fake1.published)

    # 重启：新 Fake（内存清空，但模拟远端持久——复制远端状态）+ 新 Publisher，同一 store
    fake2 = FakeGitProvider()
    fake2._published = dict(fake1._published)
    fake2._next_remote_id = fake1._next_remote_id
    pub2 = Publisher(fake2, store, repo="RepoSage")
    store._record_run(_run(run_id="run-b"))
    plan2 = pub2.build_plan(_run(run_id="run-b"), [f1], mode="publish")
    await pub2.publish(plan2)
    # DB remote_comment_id 映射兜底：新 Fake 预填历史 remote_id → 复用，零新增
    assert len(fake2.published) == n_published  # 未新增评论
    assert n_published > 0
    # 且 remote_id 与第一次一致
    first_rid = {c.remote_comment_id for c in plan1.comments}
    second_rid = {c.remote_comment_id for c in plan2.comments}
    assert first_rid == second_rid


@pytest.mark.asyncio
async def test_partial_failure_then_recover():
    """部分失败（DoD）：必要评论失败 → partial；重跑（去注入）→ published/completed。"""
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])
    fake.failed_comment_ids.add("summary")  # summary 是必要评论，失败 → partial
    plan = pub.build_plan(_run(), [f1], mode="publish")
    await pub.publish(plan)
    assert plan.status is PublishPlanStatus.PARTIAL
    # 可恢复计划可被加载
    recoverable = await store.load_recoverable_plans("run-1")
    assert any(p.plan_id == plan.plan_id for p in recoverable)
    # 重跑：去掉注入，同 plan 幂等恢复
    fake.failed_comment_ids.clear()
    await pub.publish(plan)
    assert plan.status in (PublishPlanStatus.PUBLISHED, PublishPlanStatus.COMPLETED)


@pytest.mark.asyncio
async def test_missing_provider_result_marks_partial():
    """V1-e 返工 P0：Provider 漏回必要评论 → 视为失败 → partial，不推进 watermark。"""
    from reposage.domain.run import PublishCommentResult

    class _PartialProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def publish_comments(self, plan):
            self.calls += 1
            # 只返回 summary，漏掉 inline 评论（模拟部分响应）
            return {
                "summary": PublishCommentResult(comment_id="summary", status="published", remote_comment_id=1)
            }

        async def delete_comment(self, request):
            return True

    fake = _PartialProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])  # type: ignore[arg-type]
    plan = pub.build_plan(_run(), [f1], mode="publish")
    await pub.publish(plan)
    assert plan.status is PublishPlanStatus.PARTIAL  # 漏回 inline → 不推进 published
    assert plan.committed_watermark is None  # 未推进（未进入 published 分支）


@pytest.mark.asyncio
async def test_cleanup_failure_marks_cleanup_pending():
    """supersede 清理失败（P0-R2-2）：plan=cleanup_pending，committed_watermark 已推进不阻塞。"""
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    f2 = _finding("f2", path="src/a.py", line=4)  # 旧 run 额外缺陷（第二轮修复）
    pub, store = _publisher_with_findings(fake, [f1, f2])
    plan = pub.build_plan(_run(), [f1, f2], mode="publish")
    await pub.publish(plan)  # 第一次正常发布
    f2_rid = next(c.remote_comment_id for c in plan.comments if c.finding_occurrence_id == "f2")

    # 第二轮：只剩 f1，f2 评论被 supersede 清理；注入删除失败 → cleanup_pending
    fake.failed_delete_ids = {f2_rid}
    store._record_run(_run(run_id="run-b"))
    plan2 = pub.build_plan(_run(run_id="run-b"), [f1], mode="publish")
    await pub.publish(plan2)
    # committed_watermark 已推进（不因清理失败阻塞），plan=cleanup_pending
    assert plan2.status is PublishPlanStatus.CLEANUP_PENDING
    assert plan2.committed_watermark is not None


@pytest.mark.asyncio
async def test_supersede_cleanup_deletes_stale_comments():
    """supersede 清理：旧 plan 有、新 plan 没有的评论被删除；复用评论保留（不误删）。"""
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    f2 = _finding("f2", path="src/a.py", line=4)  # 旧 run 的额外缺陷
    pub, store = _publisher_with_findings(fake, [f1, f2])
    plan = pub.build_plan(_run(), [f1, f2], mode="publish")
    await pub.publish(plan)
    f2_rid = next(c.remote_comment_id for c in plan.comments if c.finding_occurrence_id == "f2")

    # 第二轮：只剩 f1（f2 已修复），f2 评论应被 supersede 删除，f1 复用保留
    store._record_run(_run(run_id="run-b"))
    plan2 = pub.build_plan(_run(run_id="run-b"), [f1], mode="publish")
    await pub.publish(plan2)
    assert plan2.status is PublishPlanStatus.COMPLETED
    remaining_ids = {r["remote_comment_id"] for r in fake.published}
    assert f2_rid not in remaining_ids  # 旧评论 f2 被删除
    f1_rid = next(c.remote_comment_id for c in plan2.comments if c.finding_occurrence_id == "f1")
    assert f1_rid in remaining_ids  # 复用评论 f1 保留


@pytest.mark.asyncio
async def test_incremental_plan_does_not_supersede_stale_comments():
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    f2 = _finding("f2", path="src/a.py", line=4)
    pub, store = _publisher_with_findings(fake, [f1, f2])
    plan = pub.build_plan(_run(), [f1, f2], mode="publish")
    await pub.publish(plan)
    f2_rid = next(c.remote_comment_id for c in plan.comments if c.finding_occurrence_id == "f2")
    store._record_run(_run(run_id="run-b"))
    plan2 = pub.build_plan(
        _run(run_id="run-b"),
        [f1],
        mode="publish",
        allow_supersede_cleanup=False,
    )
    await pub.publish(plan2)
    remaining_ids = {r["remote_comment_id"] for r in fake.published}
    assert f2_rid in remaining_ids
    assert plan2.status is PublishPlanStatus.COMPLETED
    assert plan2.committed_watermark is not None


@pytest.mark.asyncio
async def test_cleanup_pending_resume_retries_delete(tmp_path):
    """V1-e 返工 P0：cleanup_pending 跨进程恢复（关库重开），重试失败 cleanup operation。"""
    from reposage.providers.git.fake import FakeGitProvider as FG

    db_path = tmp_path / "p.db"
    fake = FG()
    f1 = _finding("f1", path="src/a.py", line=3)
    f2 = _finding("f2", path="src/a.py", line=4)  # 旧 run 额外缺陷
    store = SqliteStorage(db_path)
    store._record_run(_run())
    store._record_findings([f1, f2])
    pub = Publisher(fake, store, repo="RepoSage")
    plan = pub.build_plan(_run(), [f1, f2], mode="publish")
    await pub.publish(plan)
    f2_rid = next(c.remote_comment_id for c in plan.comments if c.finding_occurrence_id == "f2")

    # 第二轮：只剩 f1，f2 评论被清理；注入删除失败 → cleanup_pending
    fake.failed_delete_ids = {f2_rid}
    store._record_run(_run(run_id="run-b"))
    plan2 = pub.build_plan(_run(run_id="run-b"), [f1], mode="publish")
    await pub.publish(plan2)
    assert plan2.status is PublishPlanStatus.CLEANUP_PENDING

    # 跨进程恢复：关闭 store，重开新连接 + 新 Publisher + 新 Fake
    store.close()
    fake.failed_delete_ids.clear()
    store2 = SqliteStorage(db_path)
    pub3 = Publisher(fake, store2, repo="RepoSage")
    resumed = await store2.load_publish_plan(plan2.plan_id)
    assert resumed is not None and resumed.status is PublishPlanStatus.CLEANUP_PENDING
    await pub3.publish(resumed)
    assert resumed.status is PublishPlanStatus.COMPLETED
    # 旧评论 f2 真正被删除
    remaining_ids = {r["remote_comment_id"] for r in fake.published}
    assert f2_rid not in remaining_ids


@pytest.mark.asyncio
async def test_delete_comment_rejects_marker_mismatch():
    """V1-e 返工 P1：删除请求 marker 不匹配 → 拒绝删除（越权防护）。"""
    from reposage.domain.run import DeleteCommentRequest

    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, _ = _publisher_with_findings(fake, [f1])
    plan = pub.build_plan(_run(), [f1], mode="publish")
    await pub.publish(plan)
    rid = next(iter(fake.published))["remote_comment_id"]

    # 错误 marker → 拒绝删除
    bad = DeleteCommentRequest(
        remote_comment_id=rid, pr_identity="RepoSage#1", expected_marker="<!-- reposage:wrong -->"
    )
    assert await fake.delete_comment(bad) is False
    # 评论仍在
    assert any(r["remote_comment_id"] == rid for r in fake.published)


def test_sqlite_migration_adds_columns():
    """V1-e 返工 P1 / 四轮 P1：迁移补 pr_identity/stable_key/target_head_sha/committed_watermark/lease 列。"""

    db = SqliteStorage(":memory:")
    # 模拟旧库：新库已含列，验证迁移幂等（不抛错）
    cols = {r["name"] for r in db._query("PRAGMA table_info(publish_plans)")}
    assert "pr_identity" in cols
    assert "target_head_sha" in cols
    assert "committed_watermark" in cols
    assert "lease_owner" in cols
    assert "lease_until" in cols
    ccols = {r["name"] for r in db._query("PRAGMA table_info(published_comments)")}
    assert "stable_key" in ccols
    # user_version 已迁移到 v5（V2-D：needs_evidence）
    assert db._query("PRAGMA user_version")[0][0] == 5


def test_sqlite_migration_from_old_schema(tmp_path):
    """V1-e 返工 P1（审查 blocking 覆盖）：旧库（缺列且无 UNIQUE）升级后可正常 publish。"""
    import sqlite3

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, external_ref TEXT, base_sha TEXT, head_sha TEXT,
            strategy TEXT, status TEXT, publish_status TEXT, warnings_json TEXT, config_hash TEXT,
            started_at TEXT, finished_at TEXT);
        CREATE TABLE findings (finding_occurrence_id TEXT PRIMARY KEY, run_id TEXT, fingerprint TEXT,
            cross_run_match_key TEXT, cluster_id TEXT, title TEXT, severity TEXT, confidence REAL,
            category TEXT, claimed_path TEXT, claimed_start INTEGER, claimed_end INTEGER,
            canonical_path TEXT, canonical_start INTEGER, canonical_end INTEGER, status TEXT,
            evidence_json TEXT, sources_json TEXT, is_outside_diff INTEGER,
            UNIQUE(run_id, fingerprint));
        CREATE TABLE finding_versions (id INTEGER PRIMARY KEY AUTOINCREMENT,
            finding_occurrence_id TEXT REFERENCES findings(finding_occurrence_id),
            from_status TEXT, to_status TEXT, actor TEXT, reason TEXT, at TEXT);
        CREATE TABLE publish_plans (plan_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
            status TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'dry_run', watermark TEXT);
        CREATE TABLE publish_operations (op_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES publish_plans(plan_id),
            kind TEXT NOT NULL, status TEXT NOT NULL, detail TEXT);
        CREATE TABLE published_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL REFERENCES publish_plans(plan_id), comment_id TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 1, finding_occurrence_id TEXT, fingerprint TEXT,
            kind TEXT NOT NULL, path TEXT, line INTEGER, body TEXT NOT NULL,
            marker TEXT NOT NULL DEFAULT '', remote_comment_id INTEGER, status TEXT NOT NULL DEFAULT 'prepared');
        """
    )
    conn.close()

    # 升级：SqliteStorage 迁移补列 + 唯一索引，不抛错
    store = SqliteStorage(db_path)
    ccols = {r["name"] for r in store._query("PRAGMA table_info(published_comments)")}
    assert "stable_key" in ccols
    assert store._query("PRAGMA user_version")[0][0] == 5
    idx = store._query("PRAGMA index_list(published_comments)")
    assert any("plan_comment" in r["name"] for r in idx)
    # v1→v2：watermark 列已拆为 target_head_sha + committed_watermark + lease 列
    pcols = {r["name"] for r in store._query("PRAGMA table_info(publish_plans)")}
    assert "target_head_sha" in pcols
    assert "committed_watermark" in pcols
    assert "lease_owner" in pcols
    assert "lease_until" in pcols


@pytest.mark.asyncio
async def test_finding_status_transitioned():
    """Saga 成功推进 Finding → published；失败 → publish_failed。"""
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])
    plan = pub.build_plan(_run(), [f1], mode="publish")
    await pub.publish(plan)
    row = store._query(
        "SELECT status FROM findings WHERE finding_occurrence_id = ?", ("f1",)
    )[0]
    assert row["status"] == "published"


@pytest.mark.asyncio
async def test_watermark_is_head_sha():
    """target_head_sha = 候选 head SHA；committed_watermark 发布成功后 = head_sha（V1-e 四轮 P1）。"""
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, _ = _publisher_with_findings(fake, [f1])
    plan = pub.build_plan(_run(), [f1], mode="publish")
    await pub.publish(plan)
    assert plan.target_head_sha == "abc1234"
    assert plan.committed_watermark == "abc1234"  # 发布成功后推进
    assert plan.committed_watermark != "run-1"


def test_comment_body_redacts_secrets():
    """评论文本脱敏：title/explanation/suggestion 中凭证被清洗（10 §7）。"""
    fake = FakeGitProvider()
    pub, _ = _publisher(fake)
    f = _finding("f1", path="src/a.py", line=3)
    f.title = "密钥 sk-abcdef1234567890 泄漏"
    f.explanation = "API key: sk-abcdef1234567890 被硬编码"
    f.suggestion = "改用环境变量"
    plan = pub.build_plan(_run(), [f], mode="dry_run")
    inline = next(c for c in plan.comments if c.kind is CommentKind.INLINE)
    assert "sk-abcdef1234567890" not in inline.body
    assert "<redacted>" in inline.body


def test_redact_covers_common_secret_forms():
    """脱敏覆盖多种凭证形态（安全审查 HIGH 闭合）。"""
    from reposage.publishing.publisher import _redact

    cases = [
        ("ghp_1234567890abcdef", "ghp_"),  # GitHub PAT
        ("AKIA1234567890ABCDEF", "AKIA"),  # AWS access key
        ("xoxb-1234567890-abcdef", "xoxb-"),  # Slack bot token
        ("AIzaSy1234567890abcdef", "AIza"),  # Google API key
        ('api_key="supersecretvalue"', "supersecretvalue"),
        ("password=hardcoded123", "hardcoded123"),
        ("postgres://user:p@ssw0rd@host/db", "p@ssw0rd"),
        ("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----", "abc"),
    ]
    for text, secret in cases:
        assert secret not in _redact(text), f"{text!r} 未脱敏"


@pytest.mark.asyncio
async def test_db_comments_unique_no_duplicate():
    """幂等落库：重复 publish 不产生重复评论行（UNIQUE(plan_id, comment_id)）。"""
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])
    plan = pub.build_plan(_run(), [f1], mode="publish")
    await pub.publish(plan)
    plan2 = pub.build_plan(_run(), [f1], mode="publish")
    plan2.plan_id = plan.plan_id
    await pub.publish(plan2)
    rows = store._query(
        "SELECT COUNT(*) AS n FROM published_comments WHERE plan_id = ?", (plan.plan_id,)
    )
    assert rows[0]["n"] == len(plan.comments)  # 无重复行


@pytest.mark.asyncio
async def test_superseded_comment_not_reused_three_rounds():
    """V1-e 三轮 P0：f1+f2 → 删 f2 → f2 再出现，不得复用已删除 remote ID。

    这是"假成功"根因的回归测试：DB 中删除后的评论必须收敛为 superseded，
    跨 run 幂等查询只返回 status=published 的评论，因此第三轮 f2 会真实创建新评论。
    """
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    f2 = _finding("f2", path="src/a.py", line=4)
    pub, store = _publisher_with_findings(fake, [f1, f2])

    # 第一轮：发布 f1 + f2
    plan1 = pub.build_plan(_run(), [f1, f2], mode="publish")
    await pub.publish(plan1)
    assert plan1.status is PublishPlanStatus.COMPLETED
    f2_rid_1 = next(c.remote_comment_id for c in plan1.comments if c.finding_occurrence_id == "f2")

    # 第二轮：只剩 f1（f2 已修复）→ f2 被 supersede 删除
    store._record_run(_run(run_id="run-b"))
    plan2 = pub.build_plan(_run(run_id="run-b"), [f1], mode="publish")
    await pub.publish(plan2)
    assert plan2.status is PublishPlanStatus.COMPLETED
    # DB 中 f2 评论状态已收敛为 superseded
    db_row = store._query(
        "SELECT status FROM published_comments WHERE remote_comment_id = ?", (f2_rid_1,)
    )[0]
    assert db_row["status"] == "superseded"

    # 第三轮：f2 再次出现 → 不能复用已删除的 remote ID，必须真实创建/恢复
    store._record_run(_run(run_id="run-c"))
    plan3 = pub.build_plan(_run(run_id="run-c"), [f1, f2], mode="publish")
    await pub.publish(plan3)
    assert plan3.status is PublishPlanStatus.COMPLETED
    f2_rid_3 = next(c.remote_comment_id for c in plan3.comments if c.finding_occurrence_id == "f2")
    assert f2_rid_3 != f2_rid_1  # 新 remote ID，非复用已删除 ID
    # Fake 远端也确实存在新评论，且旧 ID 已不在远端
    assert fake.remote_body(f2_rid_3) is not None
    assert fake.remote_body(f2_rid_1) is None
    # 跨 run 幂等查询不再返回已删除 ID
    history = await store.load_published_remote_ids("RepoSage#1")
    assert f2_rid_1 not in history.values()


@pytest.mark.asyncio
async def test_summary_body_updates_across_runs():
    """V1-e 三轮 P0：summary 内容变化后 remote ID 不变，但远端 body 更新。"""
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])

    plan1 = pub.build_plan(_run(), [f1], mode="publish")
    await pub.publish(plan1)
    summary_rid = next(c.remote_comment_id for c in plan1.comments if c.kind is CommentKind.SUMMARY)
    old_body = fake.remote_body(summary_rid)

    # 第二轮：新增一条 accepted finding → summary 数量变化
    f2 = _finding("f2", path="src/a.py", line=5)
    store._record_run(_run(run_id="run-b"))
    store._record_findings([f2])
    plan2 = pub.build_plan(_run(run_id="run-b"), [f1, f2], mode="publish")
    await pub.publish(plan2)
    summary_rid_2 = next(c.remote_comment_id for c in plan2.comments if c.kind is CommentKind.SUMMARY)
    assert summary_rid_2 == summary_rid  # 稳定槽位，ID 不变
    new_body = fake.remote_body(summary_rid)
    assert new_body != old_body  # 远端 body 已更新为新内容
    assert "2 条 accepted" in new_body


@pytest.mark.asyncio
async def test_finding_body_updates_across_runs():
    """V1-e 三轮 P0：finding 说明/建议变化后 remote ID 不变，远端 body 更新。"""
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])

    plan1 = pub.build_plan(_run(), [f1], mode="publish")
    await pub.publish(plan1)
    inline = next(c for c in plan1.comments if c.kind is CommentKind.INLINE)
    old_body = fake.remote_body(inline.remote_comment_id)

    # 第二轮：同 cross_run_match_key 的 finding，但 suggestion 变化
    f1b = _finding("f1", path="src/a.py", line=3)
    f1b.suggestion = "改用 AST 白名单并拒绝动态执行"
    store._record_run(_run(run_id="run-b"))
    plan2 = pub.build_plan(_run(run_id="run-b"), [f1b], mode="publish")
    await pub.publish(plan2)
    inline2 = next(c for c in plan2.comments if c.kind is CommentKind.INLINE)
    assert inline2.remote_comment_id == inline.remote_comment_id  # ID 不变
    new_body = fake.remote_body(inline.remote_comment_id)
    assert new_body != old_body  # body 更新
    assert "AST 白名单" in new_body


@pytest.mark.asyncio
async def test_provider_extra_comment_id_marks_partial():
    """V1-e 三轮 P2：Provider 返回计划之外的 comment_id → 记 warning 并置 partial。"""
    from reposage.domain.run import PublishCommentResult

    class _ExtraProvider:
        def __init__(self) -> None:
            self.base = FakeGitProvider()

        async def publish_comments(self, plan):
            results = await self.base.publish_comments(plan)
            # 额外注入一个计划外的 comment_id
            results["ghost-comment"] = PublishCommentResult(
                comment_id="ghost-comment", status="published", remote_comment_id=9999
            )
            return results

        async def delete_comment(self, request):
            return await self.base.delete_comment(request)

    fake = _ExtraProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])  # type: ignore[arg-type]
    plan = pub.build_plan(_run(), [f1], mode="publish")
    await pub.publish(plan)
    assert plan.status is PublishPlanStatus.PARTIAL  # 额外 ID → 不 completed
    assert any("ghost-comment" in w for w in plan.warnings)


# ---- V1-e 四轮：崩溃安全 / 并发 / 外部删除 / fail-closed / 迁移优先级 ----


@pytest.mark.asyncio
async def test_crash_after_published_before_cleanup_recovers(tmp_path):
    """四轮 P0（崩溃点 1）：plan 已 published + cleanup op PENDING，远程删除前崩溃 → 恢复清理。

    模拟：publish 执行到 record_plan_published_with_cleanup 后、_run_cleanup 前中断，
    关库重开后 recoverable 查询能找到 published+pending cleanup，恢复后清理完成。
    """
    from reposage.publishing.publisher import Publisher

    db_path = tmp_path / "c1.db"
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    f2 = _finding("f2", path="src/a.py", line=4)  # 旧 run 额外缺陷
    store = SqliteStorage(db_path)
    store._record_run(_run())
    store._record_findings([f1, f2])
    pub = Publisher(fake, store, repo="RepoSage")
    plan = pub.build_plan(_run(), [f1, f2], mode="publish")
    await pub.publish(plan)  # 第一轮正常发布 f1+f2
    f2_rid = next(c.remote_comment_id for c in plan.comments if c.finding_occurrence_id == "f2")

    # 模拟第二轮崩溃：直接手工构造「published + pending cleanup op」状态（跳过真实发布）
    store._record_run(_run(run_id="run-b"))
    plan2 = pub.build_plan(_run(run_id="run-b"), [f1], mode="publish")
    store._record_publish_plan(plan2)
    superseded = [
        t
        for t in await store.load_superseded_comment_ids("RepoSage#1", exclude_plan_id=plan2.plan_id)
        if t.expected_marker not in {c.marker for c in plan2.comments}
    ]
    from reposage.domain.enums import PublishOperationKind, PublishOperationStatus
    from reposage.domain.run import PublishOperation

    op = PublishOperation(
        op_id="op-crash1",
        plan_id=plan2.plan_id,
        kind=PublishOperationKind.SUPERSEDE_CLEANUP,
        status=PublishOperationStatus.PENDING,
        detail=json.dumps([t.model_dump() for t in superseded], ensure_ascii=False),
    )
    # 模拟 Worker claim 后落库 published + pending cleanup，随后崩溃（不 release lease）
    await store.claim_plan(plan2.plan_id, "worker-crash1", pub._lease_until_str(), pub._now_str())
    await store.record_plan_published_with_cleanup(plan2.plan_id, "abc1234", op, "worker-crash1")

    # 崩溃恢复：关库重开；模拟时间流逝使崩溃 Worker 租约过期，恢复 Worker 才能 claim
    store.close()
    store2 = SqliteStorage(db_path)
    store2._conn.execute(
        "UPDATE publish_plans SET lease_until = '2000-01-01T00:00:00' WHERE plan_id = ?",
        (plan2.plan_id,),
    )
    store2._conn.commit()
    recovered = await store2.load_latest_recoverable_publish_plan("RepoSage#1")
    assert recovered is not None
    assert recovered.plan_id == plan2.plan_id  # 找到 published+pending cleanup 的 plan

    pub3 = Publisher(fake, store2, repo="RepoSage")
    result = await pub3.publish(recovered)
    assert result.status is PublishPlanStatus.COMPLETED  # 清理恢复完成
    assert f2_rid not in {r["remote_comment_id"] for r in fake.published}  # 旧评论被删


@pytest.mark.asyncio
async def test_delete_comment_raises_converges_to_cleanup_pending(tmp_path):
    """四轮 P0（崩溃点 2/3/4）：delete_comment 抛异常（非返回 False）→ cleanup_pending 可恢复。"""
    from reposage.publishing.publisher import Publisher

    db_path = tmp_path / "c2.db"
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    f2 = _finding("f2", path="src/a.py", line=4)
    store = SqliteStorage(db_path)
    store._record_run(_run())
    store._record_findings([f1, f2])
    pub = Publisher(fake, store, repo="RepoSage")
    plan = pub.build_plan(_run(), [f1, f2], mode="publish")
    await pub.publish(plan)
    f2_rid = next(c.remote_comment_id for c in plan.comments if c.finding_occurrence_id == "f2")

    # 第二轮：只剩 f1，f2 清理时 delete_comment 抛异常 → 不崩溃，收敛 cleanup_pending
    fake.raise_on_delete_ids = {f2_rid}
    store._record_run(_run(run_id="run-b"))
    plan2 = pub.build_plan(_run(run_id="run-b"), [f1], mode="publish")
    await pub.publish(plan2)
    assert plan2.status is PublishPlanStatus.CLEANUP_PENDING  # 异常收敛，不遗留 running
    # 关库重开恢复：去异常注入后清理完成
    store.close()
    fake.raise_on_delete_ids.clear()
    store2 = SqliteStorage(db_path)
    recovered = await store2.load_latest_recoverable_publish_plan("RepoSage#1")
    assert recovered is not None
    pub3 = Publisher(fake, store2, repo="RepoSage")
    result = await pub3.publish(recovered)
    assert result.status is PublishPlanStatus.COMPLETED
    assert f2_rid not in {r["remote_comment_id"] for r in fake.published}


@pytest.mark.asyncio
async def test_cleanup_payload_corrupt_fail_closed():
    """四轮 P1：cleanup payload 损坏（非法 JSON）→ fail-closed，不声称完成。"""
    from reposage.domain.enums import PublishOperationKind, PublishOperationStatus
    from reposage.domain.run import PublishOperation

    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])

    # 构造一个 cleanup_pending plan + 损坏 payload 的 op（内存状态直接设为 cleanup_pending）
    plan = pub.build_plan(_run(), [f1], mode="publish")
    plan.status = PublishPlanStatus.CLEANUP_PENDING
    op = PublishOperation(
        op_id="op-bad",
        plan_id=plan.plan_id,
        kind=PublishOperationKind.SUPERSEDE_CLEANUP,
        status=PublishOperationStatus.FAILED,
        detail="{not valid json",
    )
    plan.operations.append(op)

    result = await pub.publish(plan)
    # fail-closed：payload 损坏 → 不 completed，仍 cleanup_pending
    assert result.status is PublishPlanStatus.CLEANUP_PENDING
    db_op = store._query(
        "SELECT status FROM publish_operations WHERE op_id = ?", ("op-bad",)
    )[0]
    assert db_op["status"] == "failed"


@pytest.mark.asyncio
async def test_cleanup_payload_null_field_fail_closed():
    """四轮 P1：cleanup payload 字段校验失败（缺 remote_comment_id）→ fail-closed。"""
    from reposage.domain.enums import PublishOperationKind, PublishOperationStatus
    from reposage.domain.run import PublishOperation

    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])
    plan = pub.build_plan(_run(), [f1], mode="publish")
    plan.status = PublishPlanStatus.CLEANUP_PENDING
    op = PublishOperation(
        op_id="op-nullfield",
        plan_id=plan.plan_id,
        kind=PublishOperationKind.SUPERSEDE_CLEANUP,
        status=PublishOperationStatus.FAILED,
        detail='[{"pr_identity": "x", "expected_marker": "m"}]',  # 缺 remote_comment_id
    )
    plan.operations.append(op)

    result = await pub.publish(plan)
    assert result.status is PublishPlanStatus.CLEANUP_PENDING


@pytest.mark.asyncio
async def test_concurrent_claim_single_worker():
    """五轮 P0：两个 Worker 真正并发争抢同一 plan，只有一个执行远程副作用。

    用 publish_gate 让 Worker A 进入 Provider 后暂停；Worker B 在 A 租约仍有效时
    同时调用 publish，断言 B claim 失败、Provider 总调用数仍为 1。
    """
    import asyncio

    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])

    gate = asyncio.Event()
    fake.publish_gate = gate  # Worker A 进入 publish_comments 后暂停

    plan = pub.build_plan(_run(), [f1], mode="publish")
    plan_id = plan.plan_id

    # Worker A：claim 成功，进入 Provider 后阻塞在 gate
    task_a = asyncio.create_task(pub.publish(plan))
    # 等待 A 进入 publish_comments（说明已 claim 成功）
    await asyncio.sleep(0.05)
    assert fake.publish_call_count == 1  # A 已进入 Provider

    # Worker B：同一 plan（同一 plan_id），在 A 租约仍有效时并发调用 publish
    plan_b = pub.build_plan(_run(run_id="run-b"), [f1], mode="publish")
    plan_b.plan_id = plan_id  # 强制复用同一 plan_id，模拟争抢同一 plan
    await pub.publish(plan_b)
    # B claim 失败（A 租约未过期），返回当前 DB 状态，未执行远程副作用
    assert fake.publish_call_count == 1  # Provider 总调用数仍为 1

    # 释放 A，让其完成
    gate.set()
    result_a = await task_a
    assert result_a.status is PublishPlanStatus.COMPLETED
    assert fake.publish_call_count == 1  # A 完成后仍只有一次远程发布


@pytest.mark.asyncio
async def test_lease_released_after_partial_allows_immediate_retry():
    """五轮 P0：partial 结束后释放租约，可立即被重试，不等待租期。"""
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])

    # 第一次：summary 失败 → partial（正常结束路径应释放租约）
    fake.failed_comment_ids.add("summary")
    plan = pub.build_plan(_run(), [f1], mode="publish")
    await pub.publish(plan)
    assert plan.status is PublishPlanStatus.PARTIAL
    # 租约已释放（lease_owner 为空）
    row = store._query(
        "SELECT lease_owner, lease_until FROM publish_plans WHERE plan_id = ?", (plan.plan_id,)
    )[0]
    assert row["lease_owner"] is None and row["lease_until"] is None

    # 立即重试（去注入），无需等租期 → completed
    fake.failed_comment_ids.clear()
    recovered = await store.load_latest_recoverable_publish_plan("RepoSage#1")
    assert recovered is not None
    result = await pub.publish(recovered)
    assert result.status is PublishPlanStatus.COMPLETED


@pytest.mark.asyncio
async def test_expired_lease_takeover_fences_old_worker():
    """五轮 P0：A 租约过期、B 接管后，A 的旧 fencing token 无法覆盖 B 的状态。"""
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])

    # 模拟 A 持有已过期租约（lease_owner=A，lease_until 已过去）
    plan = pub.build_plan(_run(), [f1], mode="publish")
    store._record_publish_plan(plan)
    store._conn.execute(
        "UPDATE publish_plans SET lease_owner = 'worker-A', lease_until = '2000-01-01T00:00:00' "
        "WHERE plan_id = ?",
        (plan.plan_id,),
    )
    store._conn.commit()

    # B 接管：claim 成功（A 租约已过期），推进到 completed
    result_b = await pub.publish(plan)
    assert result_b.status is PublishPlanStatus.COMPLETED

    # A 的旧 fencing token 尝试写状态：抛 LeaseLostError（租约已被 B 接管），B 状态不被覆盖
    from reposage.domain.run import LeaseLostError

    with pytest.raises(LeaseLostError):
        await store.update_plan_status(plan.plan_id, PublishPlanStatus.PARTIAL, "worker-A")
    row = store._query(
        "SELECT status FROM publish_plans WHERE plan_id = ?", (plan.plan_id,)
    )[0]
    assert row["status"] == "completed"  # A 的旧写未覆盖 B 的 completed


@pytest.mark.asyncio
async def test_mark_plan_obsolete_skips_leased_plan():
    """六轮复验 should-fix：mark_plan_obsolete 只废弃无活跃租约的 plan，不干扰持租约的 Worker。"""
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])

    plan = pub.build_plan(_run(), [f1], mode="publish")
    store._record_publish_plan(plan)

    # 模拟另一 Worker 持活跃租约（lease_owner 非空、未过期）
    store._conn.execute(
        "UPDATE publish_plans SET lease_owner = 'worker-active', lease_until = '2999-01-01T00:00:00' "
        "WHERE plan_id = ?",
        (plan.plan_id,),
    )
    store._conn.commit()

    # mark_plan_obsolete 应跳过（不废弃持租约的 plan）
    await store.mark_plan_obsolete(plan.plan_id)
    row = store._query(
        "SELECT status FROM publish_plans WHERE plan_id = ?", (plan.plan_id,)
    )[0]
    assert row["status"] != "obsolete"  # 持租约的 plan 未被误废弃

    # 释放租约后，mark_plan_obsolete 生效
    await store.release_lease(plan.plan_id, "worker-active")
    await store.mark_plan_obsolete(plan.plan_id)
    row = store._query(
        "SELECT status FROM publish_plans WHERE plan_id = ?", (plan.plan_id,)
    )[0]
    assert row["status"] == "obsolete"


@pytest.mark.asyncio
async def test_cleanup_concurrent_claim_single_worker(tmp_path):
    """五轮 P0：cleanup 路径同样并发争抢，只有一个执行远程删除。"""
    import asyncio

    from reposage.publishing.publisher import Publisher

    db_path = tmp_path / "cc.db"
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    f2 = _finding("f2", path="src/a.py", line=4)
    store = SqliteStorage(db_path)
    store._record_run(_run())
    store._record_findings([f1, f2])
    pub = Publisher(fake, store, repo="RepoSage")
    plan = pub.build_plan(_run(), [f1, f2], mode="publish")
    await pub.publish(plan)
    f2_rid = next(c.remote_comment_id for c in plan.comments if c.finding_occurrence_id == "f2")

    # 构造 cleanup_pending plan（第二轮只剩 f1，f2 待删，注入删除失败 → cleanup_pending）
    fake.failed_delete_ids = {f2_rid}
    store._record_run(_run(run_id="run-b"))
    plan2 = pub.build_plan(_run(run_id="run-b"), [f1], mode="publish")
    await pub.publish(plan2)
    assert plan2.status is PublishPlanStatus.CLEANUP_PENDING
    fake.failed_delete_ids.clear()

    # 并发争抢 cleanup 恢复：两个 Worker 同时恢复同一 cleanup_pending plan
    gate = asyncio.Event()
    fake.delete_gate = gate  # Worker A 进入 delete_comment 后暂停
    fake.delete_call_count = 0

    recovered_a = await store.load_publish_plan(plan2.plan_id)
    recovered_b = await store.load_publish_plan(plan2.plan_id)

    # Worker A：claim 成功，进入 delete_comment 后阻塞在 gate
    task_a = asyncio.create_task(pub.publish(recovered_a))
    await asyncio.sleep(0.05)
    assert fake.delete_call_count == 1  # A 已进入删除（说明 claim 成功）

    # Worker B：同一 cleanup_pending plan 并发争抢，claim 失败（A 租约有效）
    await pub.publish(recovered_b)
    assert fake.delete_call_count == 1  # B 未执行删除，仍只有 A 一次

    # 释放 A，让其完成清理
    gate.set()
    result_a = await task_a

    assert result_a.status is PublishPlanStatus.COMPLETED
    assert fake.delete_call_count == 1  # 全程只执行一次删除
    assert f2_rid not in {r["remote_comment_id"] for r in fake.published}


@pytest.mark.asyncio
async def test_external_delete_then_stable_new_id():
    """四轮 P0：远程评论被人工删除（10 被删）→ 创建 20 → 后续稳定复用 20，不回退 10。"""
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])

    plan1 = pub.build_plan(_run(), [f1], mode="publish")
    await pub.publish(plan1)
    inline1 = next(c for c in plan1.comments if c.kind is CommentKind.INLINE)
    old_rid = inline1.remote_comment_id  # 假设 = 2（summary=1, inline=2）

    # 模拟用户在 GitHub 手工删除该评论
    for marker, (rid, _body) in list(fake._published.items()):
        if rid == old_rid:
            del fake._published[marker]

    # 第二轮：预填 old_rid，Provider 查不到 → 创建新 ID
    store._record_run(_run(run_id="run-b"))
    plan2 = pub.build_plan(_run(run_id="run-b"), [f1], mode="publish")
    await pub.publish(plan2)
    inline2 = next(c for c in plan2.comments if c.kind is CommentKind.INLINE)
    new_rid = inline2.remote_comment_id
    assert new_rid != old_rid  # 创建新评论，非复用已删除 ID
    # 旧 active 映射已收敛为 superseded
    db_row = store._query(
        "SELECT status FROM published_comments WHERE remote_comment_id = ? AND status = 'published'",
        (old_rid,),
    )
    assert db_row == []  # 旧 ID 不再 published

    # 第三轮：稳定复用 new_rid，不回退 old_rid
    store._record_run(_run(run_id="run-c"))
    plan3 = pub.build_plan(_run(run_id="run-c"), [f1], mode="publish")
    await pub.publish(plan3)
    inline3 = next(c for c in plan3.comments if c.kind is CommentKind.INLINE)
    assert inline3.remote_comment_id == new_rid
    # load_published_remote_ids 只返回 new_rid
    history = await store.load_published_remote_ids("RepoSage#1")
    assert old_rid not in history.values()
    assert new_rid in history.values()


def test_migration_dedup_keeps_published_remote_id(tmp_path):
    """四轮 P2：迁移去重保留 published+remote_id 优先（非 MAX(id) 丢弃有效映射）。"""
    import sqlite3

    db_path = tmp_path / "dup.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, external_ref TEXT, base_sha TEXT, head_sha TEXT,
            strategy TEXT, status TEXT, publish_status TEXT, warnings_json TEXT, config_hash TEXT,
            started_at TEXT, finished_at TEXT);
        CREATE TABLE findings (finding_occurrence_id TEXT PRIMARY KEY, run_id TEXT, fingerprint TEXT,
            cross_run_match_key TEXT, cluster_id TEXT, title TEXT, severity TEXT, confidence REAL,
            category TEXT, claimed_path TEXT, claimed_start INTEGER, claimed_end INTEGER,
            canonical_path TEXT, canonical_start INTEGER, canonical_end INTEGER, status TEXT,
            evidence_json TEXT, sources_json TEXT, is_outside_diff INTEGER,
            UNIQUE(run_id, fingerprint));
        CREATE TABLE finding_versions (id INTEGER PRIMARY KEY AUTOINCREMENT,
            finding_occurrence_id TEXT REFERENCES findings(finding_occurrence_id),
            from_status TEXT, to_status TEXT, actor TEXT, reason TEXT, at TEXT);
        CREATE TABLE publish_plans (plan_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
            status TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'dry_run', watermark TEXT);
        CREATE TABLE publish_operations (op_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES publish_plans(plan_id),
            kind TEXT NOT NULL, status TEXT NOT NULL, detail TEXT);
        CREATE TABLE published_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL REFERENCES publish_plans(plan_id), comment_id TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 1, finding_occurrence_id TEXT, fingerprint TEXT,
            kind TEXT NOT NULL, path TEXT, line INTEGER, body TEXT NOT NULL,
            marker TEXT NOT NULL DEFAULT '', remote_comment_id INTEGER, status TEXT NOT NULL DEFAULT 'prepared');
        """
    )
    conn.close()

    # 预填重复数据：旧行 published+remote_id=99（id 小），新行 failed（id 大）
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO publish_plans (plan_id, run_id, status, mode) VALUES ('p1', 'r1', 'completed', 'publish')")
    conn.execute("INSERT INTO runs (run_id) VALUES ('r1')")
    # 旧行：published + remote_id（有效映射）
    conn.execute(
        "INSERT INTO published_comments (plan_id, comment_id, required, kind, body, marker, remote_comment_id, status) "
        "VALUES ('p1', 'c1', 1, 'inline', 'old body', 'm1', 99, 'published')"
    )
    # 新行（重复 plan_id+comment_id）：failed，无 remote_id —— 不应取代有效映射
    conn.execute(
        "INSERT INTO published_comments (plan_id, comment_id, required, kind, body, marker, remote_comment_id, status) "
        "VALUES ('p1', 'c1', 1, 'inline', 'new body', 'm1', NULL, 'failed')"
    )
    conn.commit()
    conn.close()

    # 迁移去重后，保留 published+remote_id 的那行（id 较小的旧行）
    store = SqliteStorage(db_path)
    row = store._query(
        "SELECT remote_comment_id, status FROM published_comments WHERE plan_id = 'p1' AND comment_id = 'c1'"
    )[0]
    assert row["remote_comment_id"] == 99
    assert row["status"] == "published"


@pytest.mark.asyncio
async def test_crash_before_transition_findings_recovers_finding_status(tmp_path):
    """四轮复验 should-fix：崩溃在 published 落库后、_transition_findings 前，恢复补推进 finding 状态。

    构造：plan 已 published + pending cleanup op，但 finding 仍 accepted（未推进），
    恢复清理后 finding 应被补推进为 published（_transition_findings 幂等）。
    """
    from reposage.domain.enums import (
        CommentStatus,
        PublishOperationKind,
        PublishOperationStatus,
    )
    from reposage.domain.run import PublishCommentResult, PublishOperation
    from reposage.publishing.publisher import Publisher

    db_path = tmp_path / "c3.db"
    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    f2 = _finding("f2", path="src/a.py", line=4)
    store = SqliteStorage(db_path)
    store._record_run(_run())
    store._record_findings([f1, f2])
    pub = Publisher(fake, store, repo="RepoSage")
    plan = pub.build_plan(_run(), [f1, f2], mode="publish")
    await pub.publish(plan)

    # 第二轮：构造 published + pending cleanup 状态，但**不**推进 finding（模拟崩溃于 transition 前）
    store._record_run(_run(run_id="run-b"))
    plan2 = pub.build_plan(_run(run_id="run-b"), [f1], mode="publish")
    store._record_publish_plan(plan2)
    # 先 claim（模拟 Worker 已持有租约）
    await store.claim_plan(plan2.plan_id, "worker-crash3", pub._lease_until_str(), pub._now_str())
    # 把 plan2 的 inline 评论标记为已发布（有 remote_id），但 finding 保持 accepted
    store._record_comment_results(
        plan2.plan_id,
        {
            c.comment_id: PublishCommentResult(
                comment_id=c.comment_id,
                status=CommentStatus.PUBLISHED,
                remote_comment_id=c.remote_comment_id or (i + 100),
            )
            for i, c in enumerate(plan2.comments)
        },
        "worker-crash3",
    )
    superseded = [
        t
        for t in await store.load_superseded_comment_ids("RepoSage#1", exclude_plan_id=plan2.plan_id)
        if t.expected_marker not in {c.marker for c in plan2.comments}
    ]
    op = PublishOperation(
        op_id="op-crash3",
        plan_id=plan2.plan_id,
        kind=PublishOperationKind.SUPERSEDE_CLEANUP,
        status=PublishOperationStatus.PENDING,
        detail=json.dumps([t.model_dump() for t in superseded], ensure_ascii=False),
    )
    await store.record_plan_published_with_cleanup(plan2.plan_id, "abc1234", op, "worker-crash3")
    # 此时 f1 的 finding 仍是 accepted（未推进），模拟崩溃

    # 恢复：模拟时间流逝使崩溃 Worker 租约过期
    store.close()
    store2 = SqliteStorage(db_path)
    store2._conn.execute(
        "UPDATE publish_plans SET lease_until = '2000-01-01T00:00:00' WHERE plan_id = ?",
        (plan2.plan_id,),
    )
    store2._conn.commit()
    recovered = await store2.load_latest_recoverable_publish_plan("RepoSage#1")
    assert recovered is not None
    pub3 = Publisher(fake, store2, repo="RepoSage")
    result = await pub3.publish(recovered)
    assert result.status is PublishPlanStatus.COMPLETED
    # f1 的 finding 被补推进为 published
    f1_row = store2._query(
        "SELECT status FROM findings WHERE finding_occurrence_id = ?", ("f1",)
    )[0]
    assert f1_row["status"] == "published"


@pytest.mark.asyncio
async def test_stale_worker_fully_fenced_after_takeover():
    """五轮 P0：暂停 A → 租约过期 → B 接管完成 → 恢复 A，A 全流程被 fencing 隔离。

    编排：publish_gate 让 A 进入 publish_comments 后暂停；手动把 A 租约改为过期；
    B claim 成功并完成；释放 A 后 A 继续执行时因租约丢失抛 LeaseLostError，
    publish 捕获后返回 B 的 completed 状态，且不产生额外远端副作用。
    """
    import asyncio

    from reposage.domain.run import LeaseLostError

    fake = FakeGitProvider()
    f1 = _finding("f1", path="src/a.py", line=3)
    pub, store = _publisher_with_findings(fake, [f1])

    gate = asyncio.Event()
    fake.publish_gate = gate  # 仅第一次 publish_comments 调用阻塞（Worker A）

    plan_a = pub.build_plan(_run(), [f1], mode="publish")
    plan_id = plan_a.plan_id

    # Worker A：claim 成功，进入 publish_comments 后阻塞在 gate
    task_a = asyncio.create_task(pub.publish(plan_a))
    await asyncio.sleep(0.05)
    assert fake.publish_call_count == 1  # A 已进入 Provider（claim 成功）

    # 让 A 的租约过期（模拟 A 长时间阻塞/崩溃）
    store._conn.execute(
        "UPDATE publish_plans SET lease_until = '2000-01-01T00:00:00' WHERE plan_id = ?",
        (plan_id,),
    )
    store._conn.commit()

    # Worker B：claim 成功（A 租约已过期），第二次 publish_comments 不阻塞，完成发布
    plan_b = pub.build_plan(_run(run_id="run-b"), [f1], mode="publish")
    plan_b.plan_id = plan_id  # 复用同一 plan_id
    result_b = await pub.publish(plan_b)
    assert result_b.status is PublishPlanStatus.COMPLETED
    assert fake.publish_call_count == 2  # A 1 次 + B 1 次

    # 记录 B 完成后的状态与远端副作用基线
    b_status = store._query(
        "SELECT status FROM publish_plans WHERE plan_id = ?", (plan_id,)
    )[0]["status"]
    n_published = len(fake.published)

    # 释放 A，让其恢复运行
    gate.set()
    result_a = await task_a
    # A 恢复后：publish 捕获 LeaseLostError，返回当前 DB 状态（B 的 completed），不抛异常
    assert result_a.status is PublishPlanStatus.COMPLETED

    # 断言 A 未覆盖 B 的状态、未产生额外远端副作用
    row = store._query(
        "SELECT status FROM publish_plans WHERE plan_id = ?", (plan_id,)
    )[0]
    assert row["status"] == b_status == "completed"
    assert len(fake.published) == n_published  # 无额外远端评论
    assert fake.publish_call_count == 2  # A 恢复后未再调用远端发布

    # A 的旧 fencing token 直接写任何状态都抛 LeaseLostError
    with pytest.raises(LeaseLostError):
        await store.update_plan_status(plan_id, PublishPlanStatus.PARTIAL, "stale-token")
    with pytest.raises(LeaseLostError):
        await store.finish_cleanup(plan_id, success=True, lease_owner="stale-token")
