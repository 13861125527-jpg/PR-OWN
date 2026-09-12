"""稳定 PR 身份（V1-e / V2-E FETCH 与 Publisher 共用）。"""

from __future__ import annotations

from reposage.domain.run import ReviewRun


def pr_identity(*, repo: str, external_ref: str | None, head_sha: str) -> str:
    """repo + external_ref（PR number）；无 external_ref 时退化为 head_sha。"""
    external = external_ref or head_sha
    return f"{repo}#{external}" if repo else external


def pr_identity_for(run: ReviewRun, repo: str) -> str:
    return pr_identity(repo=repo, external_ref=run.external_ref, head_sha=run.head_sha)
