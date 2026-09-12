"""GitHub Actions adapter: local git snapshots plus GitHub PR comment publishing."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx

from reposage.domain.enums import CommentKind, CommentStatus
from reposage.domain.models import ChangeRequest, ChangeRequestSource, CommitRef
from reposage.domain.run import DeleteCommentRequest, PublishCommentResult, PublishPlan

from .local import LocalGitProvider, LocalGitSnapshotProvider


class GitHubAPIError(RuntimeError):
    """A GitHub API request failed after bounded retries."""


class GitHubActionProvider(LocalGitProvider):
    """Review an Action checkout and publish idempotent comments to one PR."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        repository: str,
        pull_number: int,
        token: str,
        api_url: str = "https://api.github.com",
        snapshot: LocalGitSnapshotProvider | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if repository.count("/") != 1:
            raise ValueError("repository 必须是 owner/name")
        if pull_number <= 0:
            raise ValueError("pull_number 必须为正整数")
        if not token:
            raise ValueError("缺少 GitHub token")
        super().__init__(Path(repo_root), snapshot=snapshot)
        self.repository = repository
        self.pull_number = pull_number
        self.repository_id = f"github:{repository}"
        self.snapshot.repository_id = self.repository_id
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            timeout=httpx.Timeout(30),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "RepoSage",
            },
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _api(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(3):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.RequestError as exc:
                if attempt == 2:
                    raise GitHubAPIError(f"GitHub 网络请求失败: {type(exc).__name__}") from exc
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            if response.status_code < 400:
                if response.status_code == 204 or not response.content:
                    return None
                return response.json()
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            message = ""
            with suppress(ValueError, AttributeError):
                message = str(response.json().get("message", ""))
            raise GitHubAPIError(f"GitHub HTTP {response.status_code}: {message[:200]}")
        raise GitHubAPIError("GitHub 请求失败")

    async def _pull(self) -> dict[str, Any]:
        data = await self._api("GET", f"/repos/{self.repository}/pulls/{self.pull_number}")
        if not isinstance(data, dict):
            raise GitHubAPIError("GitHub PR 响应格式错误")
        return data

    async def get_changes(self, ref: str) -> ChangeRequest:
        if ref and ref != str(self.pull_number):
            raise ValueError("PR 编号与 Action 事件不一致")
        pull = await self._pull()
        base_sha = str(pull.get("base", {}).get("sha", ""))
        head_sha = str(pull.get("head", {}).get("sha", ""))
        self.snapshot._check_rev(base_sha)
        self.snapshot._check_rev(head_sha)
        return ChangeRequest(
            source=ChangeRequestSource.GITHUB_PR,
            external_id=str(self.pull_number),
            base=CommitRef(sha=base_sha, label="base"),
            head=CommitRef(sha=head_sha, label="head", locked=True),
            title=str(pull.get("title") or ""),
            description=str(pull.get("body") or ""),
            author=str(pull.get("user", {}).get("login") or ""),
            is_draft=bool(pull.get("draft", False)),
            labels=[str(item.get("name")) for item in pull.get("labels", []) if item.get("name")],
            assignees=[str(item.get("login")) for item in pull.get("assignees", []) if item.get("login")],
        )

    async def _existing_comments(self) -> list[tuple[str, dict[str, Any]]]:
        issue = await self._api("GET", f"/repos/{self.repository}/issues/{self.pull_number}/comments?per_page=100")
        review = await self._api("GET", f"/repos/{self.repository}/pulls/{self.pull_number}/comments?per_page=100")
        rows: list[tuple[str, dict[str, Any]]] = []
        if isinstance(issue, list):
            rows.extend(("issue", row) for row in issue if isinstance(row, dict))
        if isinstance(review, list):
            rows.extend(("review", row) for row in review if isinstance(row, dict))
        return rows

    @staticmethod
    def _find_marker(comments: list[tuple[str, dict[str, Any]]], marker: str) -> tuple[str, int] | None:
        for comment_type, row in comments:
            if marker and marker in str(row.get("body", "")):
                try:
                    return comment_type, int(row["id"])
                except (KeyError, TypeError, ValueError):
                    continue
        return None

    async def publish_comments(self, plan: PublishPlan) -> dict[str, PublishCommentResult]:
        if plan.pr_identity != f"{self.repository}#{self.pull_number}":
            return self._all_failed(plan, "发布目标与当前 GitHub PR 不一致")
        pull = await self._pull()
        remote_head = str(pull.get("head", {}).get("sha", ""))
        if not plan.target_head_sha or remote_head != plan.target_head_sha:
            return self._all_failed(plan, "PR head 已变化，请基于最新提交重新审查")
        existing = await self._existing_comments()
        results: dict[str, PublishCommentResult] = {}
        for comment in plan.comments:
            try:
                found = self._find_marker(existing, comment.marker)
                if comment.kind is CommentKind.INLINE:
                    if not comment.path or comment.line is None:
                        raise GitHubAPIError("行内评论缺少 path/line")
                    if found:
                        kind, remote_id = found
                        if kind != "review":
                            raise GitHubAPIError("marker 类型与行内评论不一致")
                        data = await self._api("PATCH", f"/repos/{self.repository}/pulls/comments/{remote_id}", json={"body": comment.body})
                    else:
                        data = await self._api(
                            "POST",
                            f"/repos/{self.repository}/pulls/{self.pull_number}/comments",
                            json={"body": comment.body, "commit_id": plan.target_head_sha, "path": comment.path, "line": comment.line, "side": "RIGHT"},
                        )
                else:
                    if found:
                        kind, remote_id = found
                        if kind != "issue":
                            raise GitHubAPIError("marker 类型与正文评论不一致")
                        data = await self._api("PATCH", f"/repos/{self.repository}/issues/comments/{remote_id}", json={"body": comment.body})
                    else:
                        data = await self._api("POST", f"/repos/{self.repository}/issues/{self.pull_number}/comments", json={"body": comment.body})
                remote_id = int(data["id"])
                results[comment.comment_id] = PublishCommentResult(comment_id=comment.comment_id, status=CommentStatus.PUBLISHED, remote_comment_id=remote_id)
                existing.append(("review" if comment.kind is CommentKind.INLINE else "issue", data))
            except (GitHubAPIError, KeyError, TypeError, ValueError) as exc:
                results[comment.comment_id] = PublishCommentResult(comment_id=comment.comment_id, status=CommentStatus.FAILED, error=str(exc)[:300])
        return results

    @staticmethod
    def _all_failed(plan: PublishPlan, error: str) -> dict[str, PublishCommentResult]:
        return {comment.comment_id: PublishCommentResult(comment_id=comment.comment_id, status=CommentStatus.FAILED, error=error) for comment in plan.comments}

    async def delete_comment(self, request: DeleteCommentRequest) -> bool:
        if request.pr_identity != f"{self.repository}#{self.pull_number}":
            return False
        for kind, collection in (
            ("issue", f"/repos/{self.repository}/issues/{self.pull_number}/comments?per_page=100"),
            ("review", f"/repos/{self.repository}/pulls/{self.pull_number}/comments?per_page=100"),
        ):
            rows = await self._api("GET", collection)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or row.get("id") != request.remote_comment_id:
                    continue
                if request.expected_marker not in str(row.get("body", "")):
                    return False
                endpoint = f"/repos/{self.repository}/issues/comments/{request.remote_comment_id}" if kind == "issue" else f"/repos/{self.repository}/pulls/comments/{request.remote_comment_id}"
                await self._api("DELETE", endpoint)
                return True
        return False
