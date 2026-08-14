"""FakeGitProvider — 内存仓库，供测试与评测（11 §6）。

模型：文件快照 {sha: {path: content}} + 已注册的 PR 记录。
不访问网络、不触碰真实 git；支持 dry-run 与端到端测试。

Saga 能力（P1-6）：
- 幂等：同一 (plan_id, comment_id) 重复发布复用 remote_comment_id，不重复创建；
- 失败注入：`failed_comment_ids` 中的评论返回 status=failed；
- 逐条结构化返回：dict[comment_id, PublishCommentResult]。
"""

from __future__ import annotations

from difflib import unified_diff
from typing import TypedDict

from reposage.domain.enums import CommentStatus
from reposage.domain.models import ChangeRequest, ChangeRequestSource, CommitRef
from reposage.domain.run import PublishCommentResult, PublishPlan


class _PRRecord(TypedDict):
    number: int
    base: str
    head: str
    title: str
    description: str
    author: str
    is_draft: bool


class FakeGitProvider:
    """内存 Git 提供者。

    用法::

        fake = FakeGitProvider()
        fake.add_snapshot("main", {"src/a.py": "def f():\\n    return 1\\n"})
        fake.add_snapshot("feat/x", {"src/a.py": "def f():\\n    return 2\\n"})
        fake.add_pr(1, base="main", head="feat/x", title="...")
        req = await fake.get_changes("42")
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, dict[str, str]] = {}
        self._prs: dict[int, _PRRecord] = {}
        # (plan_id, comment_id) -> remote_comment_id（幂等映射，P1-6）
        self._published: dict[tuple[str, str], int] = {}
        # 注入失败的评论（status=failed）
        self.failed_comment_ids: set[str] = set()

    # ---- 测试装配 ----

    def add_snapshot(self, sha: str, files: dict[str, str]) -> None:
        self._snapshots[sha] = dict(files)

    def add_pr(
        self,
        number: int,
        *,
        base: str,
        head: str,
        title: str = "",
        description: str = "",
        author: str = "tester",
        is_draft: bool = False,
    ) -> None:
        self._prs[number] = {
            "number": number,
            "base": base,
            "head": head,
            "title": title,
            "description": description,
            "author": author,
            "is_draft": is_draft,
        }

    @property
    def published(self) -> list[dict[str, str | int]]:
        """已发布评论记录（Saga 测试断言用）。"""
        return [
            {"plan_id": k[0], "comment_id": k[1], "remote_comment_id": v}
            for k, v in self._published.items()
        ]

    # ---- Provider 契约 ----

    async def get_changes(self, ref: str) -> ChangeRequest:
        """ref: PR number（数字串）或 "base..head" 形式的本地区间。

        两种来源都返回 head.locked=True（P1-3：SHA 锁定铁律）。
        """
        if ".." in ref:
            base_sha, head_sha = ref.split("..", 1)
            if base_sha not in self._snapshots or head_sha not in self._snapshots:
                raise KeyError(f"本地快照不存在: {base_sha}..{head_sha}")
            return ChangeRequest(
                source=ChangeRequestSource.LOCAL_RANGE,
                base=CommitRef(sha=base_sha, label="base"),
                head=CommitRef(sha=head_sha, label="head", locked=True),
            )
        number = int(ref)
        if number not in self._prs:
            raise KeyError(f"PR #{number} 不存在")
        pr = self._prs[number]
        return ChangeRequest(
            source=ChangeRequestSource.GITHUB_PR,
            external_id=str(number),
            base=CommitRef(sha=pr["base"], label="base"),
            head=CommitRef(sha=pr["head"], label="head", locked=True),
            title=pr["title"],
            description=pr["description"],
            author=pr["author"],
            is_draft=pr["is_draft"],
        )

    async def get_diff(self, base_sha: str, head_sha: str, paths: list[str] | None = None) -> str:
        base_files = self._snapshots.get(base_sha, {})
        head_files = self._snapshots.get(head_sha, {})
        all_paths = sorted(set(base_files) | set(head_files))
        if paths is not None:
            all_paths = [p for p in all_paths if p in paths]

        chunks: list[str] = []
        for path in all_paths:
            old = base_files.get(path, "").splitlines(keepends=True)
            new = head_files.get(path, "").splitlines(keepends=True)
            if old == new:
                continue
            # 标准 unified diff：diff --git 头 + ---/+++ + @@ hunk（parse_unified_diff 契约）
            header = f"diff --git a/{path} b/{path}\n"
            diff = unified_diff(old, new, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="\n")
            text = "".join(diff)
            chunks.append(header + text)
        return "".join(chunks)  # chunk 末尾已带换行，避免块间空行

    async def publish_comments(self, plan: PublishPlan) -> dict[str, PublishCommentResult]:
        """Saga 发布（Fake，P1-6）：

        - 幂等：同 (plan_id, comment_id) 重复调用复用 remote_comment_id；
        - 注入失败：failed_comment_ids 中的评论返回 status=failed；
        - 其余发布并返回新 remote_comment_id。
        """
        results: dict[str, PublishCommentResult] = {}
        for comment in plan.comments:
            key = (plan.plan_id, comment.comment_id)
            if comment.comment_id in self.failed_comment_ids:
                results[comment.comment_id] = PublishCommentResult(
                    comment_id=comment.comment_id,
                    status=CommentStatus.FAILED,
                    error="injected failure",
                )
                continue
            if key in self._published:
                # 幂等复用：不重复创建
                results[comment.comment_id] = PublishCommentResult(
                    comment_id=comment.comment_id,
                    status=CommentStatus.PUBLISHED,
                    remote_comment_id=self._published[key],
                )
                continue
            remote_id = len(self._published) + 1
            self._published[key] = remote_id
            results[comment.comment_id] = PublishCommentResult(
                comment_id=comment.comment_id,
                status=CommentStatus.PUBLISHED,
                remote_comment_id=remote_id,
            )
        return results
