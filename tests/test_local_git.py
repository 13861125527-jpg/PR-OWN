"""LocalGitSnapshotProvider：锁定 SHA 只读。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from reposage.providers.git.local import LocalGitSnapshotProvider


def _git(cwd: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t.example",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t.example",
        }
    )
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.stdout.strip()


@pytest.mark.asyncio
async def test_local_git_list_and_blob(tmp_path: Path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "src/a.py")
    _git(tmp_path, "commit", "-m", "init")
    sha = _git(tmp_path, "rev-parse", "HEAD")
    provider = LocalGitSnapshotProvider(tmp_path)
    assert provider.repository_id.startswith("local:")
    paths = await provider.list_paths(sha)
    assert "src/a.py" in paths
    assert await provider.get_blob(sha, "src/a.py") == "x = 1\n"
    assert await provider.get_blob(sha, "missing.py") is None
    assert await provider.get_blob(sha, "../secret") is None
    with pytest.raises(ValueError):
        await provider.list_paths("HEAD")


@pytest.mark.asyncio
async def test_local_git_list_paths_cached_across_blobs(tmp_path: Path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("y = 2\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init")
    sha = _git(tmp_path, "rev-parse", "HEAD")
    provider = LocalGitSnapshotProvider(tmp_path)
    await provider.get_blob(sha, "src/a.py")
    await provider.get_blob(sha, "src/b.py")
    await provider.list_paths(sha)
    assert provider.ls_tree_calls == 1


@pytest.mark.asyncio
async def test_compose_local_service_produces_l3(tmp_path: Path):
    from reposage.app.compose import compose_local_service
    from reposage.domain.enums import StageName
    from reposage.providers.git.fake import FakeGitProvider
    from reposage.providers.git.local import LocalGitProvider, LocalGitSnapshotProvider
    from reposage.providers.llm.fake import FakeLLMProvider
    from reposage.storage.sqlite import SqliteStorage

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "util.py").write_text("def helper(x):\n    return x\n", encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text(
        "from src.util import helper\ndef run(x):\n    return x\n", encoding="utf-8"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "src" / "app.py").write_text(
        "from src.util import helper\ndef run(x):\n    return helper(x)\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "head")
    head = _git(tmp_path, "rev-parse", "HEAD")

    service = compose_local_service(tmp_path, llm=FakeLLMProvider(), storage=SqliteStorage(":memory:"))
    assert isinstance(service.git, LocalGitProvider)
    assert isinstance(service.snapshot_provider, LocalGitSnapshotProvider)
    assert not isinstance(service.snapshot_provider, FakeGitProvider)
    run, _ = await service.review(f"{base}..{head}")
    ctx = next(s for s in run.stages if s.stage is StageName.CONTEXT)
    assert "l3_hits=" in (ctx.detail or "")
    hits = int((ctx.detail or "").split("l3_hits=")[1].split()[0])
    assert hits > 0
    assert run.status.value in {"completed", "partial"}

