"""GitHub Actions provider contract tests using HTTP fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from reposage.domain.enums import CommentKind, CommentStatus
from reposage.domain.run import CommentPlan, DeleteCommentRequest, PublishPlan
from reposage.providers.git.github_action import GitHubActionProvider

BASE = "a" * 40
HEAD = "b" * 40


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, base_url="https://api.github.test")


def _provider(tmp_path: Path, handler: httpx.MockTransport) -> GitHubActionProvider:
    return GitHubActionProvider(
        tmp_path,
        repository="acme/demo",
        pull_number=7,
        token="test-token",
        http_client=_client(handler),
    )


def _pull() -> dict[str, object]:
    return {
        "base": {"sha": BASE},
        "head": {"sha": HEAD},
        "title": "Fix timeout",
        "body": "Keeps compatibility",
        "user": {"login": "dev"},
        "draft": False,
        "labels": [{"name": "bug"}],
        "assignees": [{"login": "reviewer"}],
    }


@pytest.mark.asyncio
async def test_get_changes_maps_locked_pr_metadata(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/demo/pulls/7"
        return httpx.Response(200, json=_pull())

    provider = _provider(tmp_path, httpx.MockTransport(handler))
    change = await provider.get_changes("7")
    assert change.external_id == "7"
    assert change.base.sha == BASE
    assert change.head.sha == HEAD
    assert change.head.locked is True
    assert change.title == "Fix timeout"
    assert change.labels == ["bug"]


@pytest.mark.asyncio
async def test_publish_creates_summary_and_inline_comment(tmp_path: Path):
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/repos/acme/demo/pulls/7" and request.method == "GET":
            return httpx.Response(200, json=_pull())
        if request.method == "GET" and request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and "/issues/7/comments" in request.url.path:
            return httpx.Response(201, json={"id": 101, "body": body["body"]})
        if request.method == "POST" and "/pulls/7/comments" in request.url.path:
            return httpx.Response(201, json={"id": 102, "body": body["body"]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = _provider(tmp_path, httpx.MockTransport(handler))
    plan = PublishPlan(
        plan_id="plan-1",
        run_id="run-1",
        pr_identity="acme/demo#7",
        mode="publish",
        target_head_sha=HEAD,
        comments=[
            CommentPlan(comment_id="summary", kind=CommentKind.SUMMARY, body="summary\n<!-- s -->", marker="<!-- s -->"),
            CommentPlan(comment_id="c-1", kind=CommentKind.INLINE, path="src/a.py", line=12, body="bug\n<!-- i -->", marker="<!-- i -->"),
        ],
    )
    result = await provider.publish_comments(plan)
    assert result["summary"].status is CommentStatus.PUBLISHED
    assert result["summary"].remote_comment_id == 101
    assert result["c-1"].remote_comment_id == 102
    inline = next(body for method, path, body in calls if method == "POST" and "/pulls/7/comments" in path)
    assert inline == {"body": "bug\n<!-- i -->", "commit_id": HEAD, "path": "src/a.py", "line": 12, "side": "RIGHT"}


@pytest.mark.asyncio
async def test_publish_updates_marker_and_rejects_changed_head(tmp_path: Path):
    head = HEAD
    patched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/demo/pulls/7":
            payload = _pull()
            payload["head"] = {"sha": head}
            return httpx.Response(200, json=payload)
        if request.url.path.endswith("/issues/7/comments"):
            return httpx.Response(200, json=[{"id": 55, "body": "old\n<!-- same -->"}])
        if request.url.path.endswith("/pulls/7/comments"):
            return httpx.Response(200, json=[])
        if request.method == "PATCH" and request.url.path.endswith("/issues/comments/55"):
            patched.append(request.url.path)
            return httpx.Response(200, json={"id": 55, "body": "new\n<!-- same -->"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = _provider(tmp_path, httpx.MockTransport(handler))
    plan = PublishPlan(
        plan_id="plan-1",
        run_id="run-1",
        pr_identity="acme/demo#7",
        target_head_sha=HEAD,
        comments=[CommentPlan(comment_id="summary", kind=CommentKind.SUMMARY, body="new\n<!-- same -->", marker="<!-- same -->")],
    )
    first = await provider.publish_comments(plan)
    assert first["summary"].remote_comment_id == 55
    assert patched
    head = "c" * 40
    second = await provider.publish_comments(plan)
    assert second["summary"].status is CommentStatus.FAILED
    assert "head" in (second["summary"].error or "")


@pytest.mark.asyncio
async def test_delete_requires_matching_pr_and_marker(tmp_path: Path):
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/issues/7/comments"):
            return httpx.Response(200, json=[{"id": 55, "body": "text\n<!-- own -->"}])
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204)
        return httpx.Response(200, json=[])

    provider = _provider(tmp_path, httpx.MockTransport(handler))
    assert not await provider.delete_comment(DeleteCommentRequest(remote_comment_id=55, pr_identity="other/repo#7", expected_marker="<!-- own -->"))
    assert not await provider.delete_comment(DeleteCommentRequest(remote_comment_id=55, pr_identity="acme/demo#7", expected_marker="<!-- wrong -->"))
    assert await provider.delete_comment(DeleteCommentRequest(remote_comment_id=55, pr_identity="acme/demo#7", expected_marker="<!-- own -->"))
    assert deleted == ["/repos/acme/demo/issues/comments/55"]
