"""本地 Git：只读快照 + 本地区间 diff。"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

from reposage.domain.enums import CommentStatus
from reposage.domain.models import ChangeRequest, ChangeRequestSource, CommitRef, is_safe_repo_path
from reposage.domain.run import DeleteCommentRequest, PublishCommentResult, PublishPlan

_REV = re.compile(r"^[0-9a-fA-F]{4,64}$")
_REF = re.compile(r"^[0-9A-Za-z._/\-]+$")


class LocalGitSnapshotProvider:
    """基于本地仓库与锁定 head SHA 的最小 Snapshot Provider。"""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.repository_id = f"local:{self.repo_root.as_posix()}"
        self._paths_cache: dict[str, frozenset[str]] = {}
        self.ls_tree_calls = 0

    def _check_rev(self, sha: str) -> str:
        if not _REV.fullmatch(sha):
            raise ValueError("head SHA 非法")
        return sha

    async def _run(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(self.repo_root), *args],
            check=False,
            capture_output=True,
        )

    async def list_paths(self, sha: str) -> list[str]:
        sha = self._check_rev(sha)
        cached = self._paths_cache.get(sha)
        if cached is not None:
            return sorted(cached)
        self.ls_tree_calls += 1
        proc = await self._run("ls-tree", "-r", "--name-only", "-z", sha)
        if proc.returncode != 0:
            self._paths_cache[sha] = frozenset()
            return []
        raw = proc.stdout.split(b"\0")
        paths: list[str] = []
        for item in raw:
            if not item:
                continue
            try:
                path = item.decode("utf-8").replace("\\", "/")
            except UnicodeDecodeError:
                continue
            if is_safe_repo_path(path):
                paths.append(path)
        self._paths_cache[sha] = frozenset(paths)
        return sorted(self._paths_cache[sha])

    async def get_blob(self, sha: str, path: str) -> str | None:
        sha = self._check_rev(sha)
        path = path.replace("\\", "/")
        if not is_safe_repo_path(path):
            return None
        listed = await self.list_paths(sha)
        if path not in listed:
            return None
        proc = await self._run("show", f"{sha}:{path}")
        if proc.returncode != 0:
            return None
        try:
            return proc.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None


class LocalGitProvider:
    """本地区间 GitProvider；快照能力委托 LocalGitSnapshotProvider。"""

    def __init__(
        self,
        repo_root: Path,
        snapshot: LocalGitSnapshotProvider | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.snapshot = snapshot or LocalGitSnapshotProvider(self.repo_root)
        self.repository_id = self.snapshot.repository_id

    async def _run(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return await self.snapshot._run(*args)

    async def _rev_parse(self, ref: str) -> str:
        if ".." in ref or not _REF.fullmatch(ref):
            raise ValueError("ref 非法")
        proc = await self._run("rev-parse", "--verify", f"{ref}^{{commit}}")
        if proc.returncode != 0:
            raise KeyError(f"本地 ref 不存在: {ref}")
        sha = proc.stdout.decode("utf-8").strip()
        if not _REV.fullmatch(sha):
            raise ValueError("head SHA 非法")
        return sha

    async def get_changes(self, ref: str) -> ChangeRequest:
        if ".." not in ref:
            raise ValueError("local git 仅支持 base..head")
        base_ref, head_ref = ref.split("..", 1)
        base_sha = await self._rev_parse(base_ref)
        head_sha = await self._rev_parse(head_ref)
        return ChangeRequest(
            source=ChangeRequestSource.LOCAL_RANGE,
            base=CommitRef(sha=base_sha, label="base"),
            head=CommitRef(sha=head_sha, label="head", locked=True),
        )

    async def get_diff(self, base_sha: str, head_sha: str, paths: list[str] | None = None) -> str:
        args = ["diff", "--no-color", "-U3", base_sha, head_sha]
        if paths:
            args.extend(["--", *paths])
        proc = await self._run(*args)
        if proc.returncode not in (0, 1):
            return ""
        try:
            return proc.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return ""

    async def get_blob(self, sha: str, path: str) -> str | None:
        return await self.snapshot.get_blob(sha, path)

    async def list_paths(self, sha: str) -> list[str]:
        return await self.snapshot.list_paths(sha)

    async def publish_comments(self, plan: PublishPlan) -> dict[str, PublishCommentResult]:
        return {
            comment.comment_id: PublishCommentResult(
                comment_id=comment.comment_id,
                status=CommentStatus.FAILED,
                error="local git 不发布远端评论",
            )
            for comment in plan.comments
        }

    async def delete_comment(self, request: DeleteCommentRequest) -> bool:
        return False
