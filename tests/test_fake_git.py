"""FakeGitProvider 测试（P1-3 本地锁定 / P1-6 Saga 幂等与失败注入）。"""

import pytest
from reposage.domain.enums import ChangeRequestSource, CommentStatus
from reposage.domain.run import CommentKind, CommentPlan, PublishPlan
from reposage.providers.git.fake import FakeGitProvider


@pytest.mark.asyncio
async def test_get_changes_github_pr_locked():
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/a.py": "def f():\n    return 1\n"})
    fake.add_snapshot("feat/x", {"src/a.py": "def f():\n    return 2\n"})
    fake.add_pr(1, base="main", head="feat/x", title="t")

    req = await fake.get_changes("1")
    assert req.source is ChangeRequestSource.GITHUB_PR
    assert req.external_id == "1"
    assert req.head.locked is True  # P1-3


@pytest.mark.asyncio
async def test_get_changes_local_range_locked():
    """P1-3：本地 base..head 也必须返回 locked=True。"""
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"a.py": "x\n"})
    fake.add_snapshot("feat/x", {"a.py": "y\n"})

    req = await fake.get_changes("main..feat/x")
    assert req.source is ChangeRequestSource.LOCAL_RANGE
    assert req.external_id is None
    assert req.head.locked is True  # SHA 锁定铁律


@pytest.mark.asyncio
async def test_get_changes_local_unknown_snapshot_raises():
    fake = FakeGitProvider()
    with pytest.raises(KeyError):
        await fake.get_changes("main..nope")


@pytest.mark.asyncio
async def test_get_diff_contains_unified_diff():
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/a.py": "def f():\n    return 1\n"})
    fake.add_snapshot("feat/x", {"src/a.py": "def f():\n    return 2\n"})

    diff = await fake.get_diff("main", "feat/x")
    assert "a/src/a.py" in diff
    assert "b/src/a.py" in diff
    assert "+" in diff
    assert "-" in diff


@pytest.mark.asyncio
async def test_get_blob_and_list_paths():
    fake = FakeGitProvider()
    fake.add_snapshot("head", {"src/a.py": "x = 1\n"})
    assert await fake.list_paths("head") == ["src/a.py"]
    assert await fake.get_blob("head", "src/a.py") == "x = 1\n"
    assert await fake.get_blob("head", "missing.py") is None
    assert await fake.get_blob("head", "../secret") is None


@pytest.mark.asyncio
async def test_publish_comments_idempotent():
    """P1-6 / V1-e 返工 P0：同 marker 重复发布不重复创建，复用 remote_comment_id。"""
    fake = FakeGitProvider()
    plan = PublishPlan(
        plan_id="p1",
        run_id="r1",
        mode="publish",
        comments=[
            CommentPlan(comment_id="c1", kind=CommentKind.INLINE, path="src/a.py", line=2,
                        body="issue", marker="<!-- reposage:Repo#1:k1:inline -->"),
            CommentPlan(comment_id="c2", kind=CommentKind.SUMMARY, body="summary",
                        marker="<!-- reposage:Repo#1:summary:summary -->"),
        ],
    )
    first = await fake.publish_comments(plan)
    second = await fake.publish_comments(plan)

    assert first["c1"].remote_comment_id == 1
    assert first["c2"].remote_comment_id == 2
    # 幂等：第二次复用，不新增
    assert second["c1"].remote_comment_id == 1
    assert second["c2"].remote_comment_id == 2
    assert len(fake.published) == 2


@pytest.mark.asyncio
async def test_publish_comments_inject_failure():
    """P1-6：可注入单条失败并返回逐条结构化状态。"""
    fake = FakeGitProvider()
    fake.failed_comment_ids = {"c2"}
    plan = PublishPlan(
        plan_id="p2",
        run_id="r1",
        mode="publish",
        comments=[
            CommentPlan(comment_id="c1", kind=CommentKind.SUMMARY, body="s"),
            CommentPlan(comment_id="c2", kind=CommentKind.INLINE, path="a.py", line=1, body="f"),
        ],
    )
    result = await fake.publish_comments(plan)
    assert result["c1"].status is CommentStatus.PUBLISHED
    assert result["c1"].remote_comment_id is not None
    assert result["c2"].status is CommentStatus.FAILED
    assert result["c2"].error == "injected failure"
    assert result["c2"].remote_comment_id is None
    # 失败的不占用 remote id，也不进入 published 记录
    assert len(fake.published) == 1


@pytest.mark.asyncio
async def test_get_changes_unknown_pr_raises():
    fake = FakeGitProvider()
    with pytest.raises(KeyError):
        await fake.get_changes("999")
