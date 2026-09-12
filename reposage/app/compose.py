"""本地审查装配：解析后的仓库根 + LocalGitSnapshotProvider → ReviewService。"""

from __future__ import annotations

from pathlib import Path

from reposage.config.settings import Settings
from reposage.domain.protocols import GitProvider, GitSnapshotProvider, LLMProvider, Storage
from reposage.providers.git.local import LocalGitProvider, LocalGitSnapshotProvider
from reposage.review.service import ReviewService
from reposage.storage.sqlite import SqliteStorage


def compose_service(
    git: GitProvider,
    snapshot: GitSnapshotProvider,
    *,
    llm: LLMProvider,
    storage: Storage | None = None,
    settings: Settings | None = None,
) -> ReviewService:
    """Shared composition root for local CLI and GitHub Actions."""
    active_settings = settings or Settings()
    return ReviewService(
        git,
        llm,
        storage if storage is not None else SqliteStorage(active_settings.storage.path),
        settings=active_settings,
        snapshot_provider=snapshot,
    )


def compose_local_service(
    repo_root: Path | str,
    *,
    llm: LLMProvider,
    storage: Storage | None = None,
    settings: Settings | None = None,
) -> ReviewService:
    """真实本地 composition root：不依赖 Fake snapshot。"""
    root = Path(repo_root).resolve()
    snapshot = LocalGitSnapshotProvider(root)
    git = LocalGitProvider(root, snapshot=snapshot)
    return compose_service(
        git,
        snapshot,
        llm=llm,
        storage=storage if storage is not None else SqliteStorage(":memory:"),
        settings=settings,
    )
