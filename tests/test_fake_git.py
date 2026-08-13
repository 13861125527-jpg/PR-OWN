"""FakeGitProvider 测试。"""

import pytest
from reposage.domain.enums import ChangeRequestSource
from reposage.domain.run import CommentKind, CommentPlan, PublishPlan
from reposage.providers.git.fake import FakeGitProvider


@pytest.mark.asyncio
async def test_get_changes_github_pr():
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/a.py": "def f():\n    return 1\n"})
    fake.add_snapshot("feat/x", {"src/a.py": "def f():\n    return 2\n"})
    fake.add_pr(1, base="main", head="feat/x", title="t")

    req = await fake.get_changes("1")
    assert req.source is ChangeRequestSource.GITHUB_PR
    assert req.external_id == "1"
    assert req.head.locked is True


@pytest.mark.asyncio
async def test_get_changes_local_range():
    fake = FakeGitProvider()
    req = await fake.get_changes("main..feat/x")
    assert req.source is ChangeRequestSource.LOCAL_RANGE
    assert req.external_id is None


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
async def test_get_diff_added_file():
    fake = FakeGitProvider()
    fake.add_snapshot("main", {})
    fake.add_snapshot("feat/x", {"new.py": "x = 1\n"})

    diff = await fake.get_diff("main", "feat/x")
    assert "new.py" in diff


@pytest.mark.asyncio
async def test_publish_comments_records_remote_ids():
    fake = FakeGitProvider()
    plan = PublishPlan(
        plan_id="p1",
        run_id="r1",
        mode="publish",
        comments=[
            CommentPlan(comment_id="c1", kind=CommentKind.INLINE, path="src/a.py", line=2, body="issue"),
            CommentPlan(comment_id="c2", kind=CommentKind.SUMMARY, body="summary"),
        ],
    )
    result = await fake.publish_comments(plan)
    assert result["c1"] == 1
    assert result["c2"] == 2
    assert len(fake.published) == 2


@pytest.mark.asyncio
async def test_get_changes_unknown_pr_raises():
    fake = FakeGitProvider()
    with pytest.raises(KeyError):
        await fake.get_changes("999")
