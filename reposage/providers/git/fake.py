"""FakeGitProvider — 内存仓库，供测试与评测（11 §6）。

模型：文件快照 {sha: {path: content}} + 已注册的 PR 记录。
不访问网络、不触碰真实 git；支持 dry-run 与端到端测试。
"""

from __future__ import annotations

from difflib import unified_diff
from typing import Any

from reposage.domain.models import (
    ChangeRequest,
    ChangeRequestSource,
    CommitRef,
)
from reposage.domain.run import PublishPlan


class FakeGitProvider:
    """内存 Git 提供者。

    用法::

        fake = FakeGitProvider()
        fake.add_snapshot("main", {"src/a.py": "def f():\\n    return 1\\n"})
        fake.add_snapshot("feat/x", {"src/a.py": "def f():\\n    return 2\\n"})
        fake.add_pr(1, base="main", head="feat/x", title="...")
        req = await fake.get_changes("42")  # 或 "42" 的数字串
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, dict[str, str]] = {}
        self._prs: dict[int, dict[str, Any]] = {}
        self._published: list[dict[str, Any]] = []

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
    def published(self) -> list[dict[str, Any]]:
        """已发布评论记录（Saga 测试断言用）。"""
        return list(self._published)

    # ---- Provider 契约 ----

    async def get_changes(self, ref: str) -> ChangeRequest:
        """ref: PR number（数字串）或 \"base..head\" 形式的本地区间。"""
        if ".." in ref:
            base_sha, head_sha = ref.split("..", 1)
            return ChangeRequest(
                source=ChangeRequestSource.LOCAL_RANGE,
                base=CommitRef(sha=base_sha, label="base"),
                head=CommitRef(sha=head_sha, label="head"),
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
            diff = unified_diff(old, new, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="")
            text = "".join(diff)
            if text:
                chunks.append(text)
        return "\n".join(chunks)

    async def publish_comments(self, plan: PublishPlan) -> dict[str, Any]:
        """Saga 发布（Fake）：直接记录并返回成功映射。"""
        result: dict[str, Any] = {}
        for comment in plan.comments:
            remote_id = len(self._published) + 1
            self._published.append(
                {
                    "plan_id": plan.plan_id,
                    "comment_id": comment.comment_id,
                    "path": comment.path,
                    "line": comment.line,
                    "body": comment.body,
                    "remote_comment_id": remote_id,
                }
            )
            result[comment.comment_id] = remote_id
        return result
