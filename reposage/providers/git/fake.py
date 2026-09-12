"""FakeGitProvider — 内存仓库，供测试与评测（11 §6）。

模型：文件快照 {sha: {path: content}} + 已注册的 PR 记录。
不访问网络、不触碰真实 git；支持 dry-run 与端到端测试。

Saga 能力（P1-6）：
- 幂等：同一 (plan_id, comment_id) 重复发布复用 remote_comment_id，不重复创建；
- 失败注入：`failed_comment_ids` 中的评论返回 status=failed；
- 逐条结构化返回：dict[comment_id, PublishCommentResult]。
"""

from __future__ import annotations

import asyncio
from difflib import unified_diff
from typing import TypedDict

from reposage.domain.enums import CommentStatus
from reposage.domain.models import ChangeRequest, ChangeRequestSource, CommitRef
from reposage.domain.run import DeleteCommentRequest, PublishCommentResult, PublishPlan


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

    def __init__(self, *, repository_id: str = "fake") -> None:
        self.repository_id = repository_id
        self._snapshots: dict[str, dict[str, str]] = {}
        self._prs: dict[int, _PRRecord] = {}
        # marker -> (remote_comment_id, body)（幂等映射，V1-e 返工 P0：键改为稳定 marker）
        self._published: dict[str, tuple[int, str]] = {}
        # 单调递增 remote id（真实 GitHub 评论 ID 删除后不复用）
        self._next_remote_id = 1
        # 注入失败的评论（status=failed）
        self.failed_comment_ids: set[str] = set()
        # 注入删除失败的 remote_comment_id（supersede 清理失败，P0-R2-2）
        self.failed_delete_ids: set[int] = set()
        # 注入 get_diff 失败的 (base, head)（V2-E 增量回退）
        self.diff_failures: set[tuple[str, str]] = set()
        # 注入 delete_comment 抛异常的 remote_comment_id（V1-e 四轮 P0：异常收敛测试）
        self.raise_on_delete_ids: set[int] = set()
        # 可注入的 publish_comments 前置闸门（V1-e 五轮并发测试：Event 暂停 Worker A）
        self.publish_gate: asyncio.Event | None = None
        # 可注入的 delete_comment 前置闸门（cleanup 并发测试）
        self.delete_gate: asyncio.Event | None = None
        # 记录 publish_comments 实际调用次数（并发测试断言只执行一次远程副作用）
        self.publish_call_count = 0
        # 记录 delete_comment 实际调用次数
        self.delete_call_count = 0

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
            {"marker": marker, "remote_comment_id": rid}
            for marker, (rid, _body) in self._published.items()
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
        if (base_sha, head_sha) in self.diff_failures:
            raise RuntimeError("injected diff failure")
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

    async def get_blob(self, sha: str, path: str) -> str | None:
        """内存快照读文件；不安全路径或缺失返回 None。"""
        from reposage.domain.models import is_safe_repo_path

        if not is_safe_repo_path(path):
            return None
        return self._snapshots.get(sha, {}).get(path)

    async def list_paths(self, sha: str) -> list[str]:
        return sorted(self._snapshots.get(sha, {}))

    async def publish_comments(self, plan: PublishPlan) -> dict[str, PublishCommentResult]:
        """Saga 发布（Fake，P1-6 / V1-e 返工 P0 / 三轮 P0 update-or-create）：

        - 注入失败：failed_comment_ids 中的评论返回 status=failed；
        - update-or-create（三轮 P0）：带 remote_comment_id 的评论若远端存在且归属
          匹配 → 更新 body（保持 ID）；远端不存在或归属不匹配 → 落回稳定 marker
          查询，命中则更新 body 复用 ID，否则创建新评论；
        - 幂等：同 marker 重复调用复用 remote_comment_id（跨 plan 稳定）。
        """
        results: dict[str, PublishCommentResult] = {}
        self.publish_call_count += 1
        # 仅第一次 publish_comments 调用阻塞（V1-e 五轮并发测试：暂停 Worker A，
        # 让 B 第二次调用能通过，从而编排"A 暂停→B 接管→A 恢复"时序）
        if self.publish_gate is not None and self.publish_call_count == 1:
            await self.publish_gate.wait()
        for comment in plan.comments:
            if comment.comment_id in self.failed_comment_ids:
                results[comment.comment_id] = PublishCommentResult(
                    comment_id=comment.comment_id,
                    status=CommentStatus.FAILED,
                    error="injected failure",
                )
                continue
            # 预填 remote_comment_id（Publisher 从 DB 映射带回）：远端存在且归属匹配 → 更新 body
            if comment.remote_comment_id is not None:
                rid = comment.remote_comment_id
                owner_marker = self._marker_for_remote_id(rid)
                if owner_marker is not None and owner_marker == comment.marker:
                    self._published[comment.marker] = (rid, comment.body)  # update body，保持 ID
                    results[comment.comment_id] = PublishCommentResult(
                        comment_id=comment.comment_id,
                        status=CommentStatus.PUBLISHED,
                        remote_comment_id=rid,
                    )
                    continue
                # 远端 ID 不存在或归属不匹配 → 落回 marker 查询/创建
            if comment.marker in self._published:
                rid, _old_body = self._published[comment.marker]
                self._published[comment.marker] = (rid, comment.body)  # update body，复用 ID
                results[comment.comment_id] = PublishCommentResult(
                    comment_id=comment.comment_id,
                    status=CommentStatus.PUBLISHED,
                    remote_comment_id=rid,
                )
                continue
            remote_id = self._next_remote_id
            self._next_remote_id += 1
            self._published[comment.marker] = (remote_id, comment.body)
            results[comment.comment_id] = PublishCommentResult(
                comment_id=comment.comment_id,
                status=CommentStatus.PUBLISHED,
                remote_comment_id=remote_id,
            )
        return results

    def _marker_for_remote_id(self, remote_comment_id: int) -> str | None:
        """按 remote_comment_id 反查 marker（update-or-create 归属校验用）。"""
        for marker, (rid, _body) in self._published.items():
            if rid == remote_comment_id:
                return marker
        return None

    def remote_body(self, remote_comment_id: int) -> str | None:
        """查询远端某评论当前正文（测试断言 update 行为用）。"""
        for _marker, (rid, body) in self._published.items():
            if rid == remote_comment_id:
                return body
        return None

    async def delete_comment(self, request: DeleteCommentRequest) -> bool:
        """删除已发布评论（supersede 清理，P0-R2-2 / V1-e 返工 P1：归属校验）。"""
        self.delete_call_count += 1
        if self.delete_gate is not None:
            await self.delete_gate.wait()
        if request.remote_comment_id in self.raise_on_delete_ids:
            raise RuntimeError(f"delete_comment boom ({request.remote_comment_id})")
        if request.remote_comment_id in self.failed_delete_ids:
            return False
        # 归属校验：remote_id 对应的评论 body 必须含预期 marker（模拟"读远程评论验证"）
        for marker, (rid, body) in list(self._published.items()):
            if rid == request.remote_comment_id:
                if request.expected_marker not in body:
                    return False  # marker 不匹配 → 拒绝删除（越权防护）
                del self._published[marker]
                return True
        return True  # 不存在 → 视为已清理（幂等）
