"""工具快照协议与适配器（23 §3.2 / DP-17）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from reposage.domain.models import ChangedFile, is_safe_repo_path
from reposage.domain.protocols import GitSnapshotProvider
from reposage.review.symbols.extract import SymbolIndex
from reposage.tools.sandbox import SandboxError, normalize_repo_path


@runtime_checkable
class ToolSnapshot(Protocol):
    """五个只读工具唯一依赖的异步快照。"""

    repository_id: str
    head_sha: str

    async def list_paths(self) -> list[str]: ...

    async def get_blob(self, path: str) -> str | None: ...


class MemoryToolSnapshot:
    """测试用内存快照。"""

    def __init__(
        self,
        blobs: dict[str, str],
        *,
        head_sha: str,
        repository_id: str = "test",
    ) -> None:
        safe: dict[str, str] = {}
        for raw, content in blobs.items():
            posix = raw.replace("\\", "/")
            if is_safe_repo_path(posix):
                safe[posix] = content
        self._blobs = safe
        self.head_sha = head_sha
        self.repository_id = repository_id

    async def list_paths(self) -> list[str]:
        return sorted(self._blobs)

    async def get_blob(self, path: str) -> str | None:
        try:
            key = normalize_repo_path(path)
        except SandboxError:
            return None
        return self._blobs.get(key)


class GitToolSnapshot:
    """GitSnapshotProvider + 锁定 SHA；内部缓存，不走 L3 cap。"""

    def __init__(
        self,
        provider: GitSnapshotProvider,
        *,
        head_sha: str,
        repository_id: str = "",
    ) -> None:
        self._git = provider
        self.head_sha = head_sha
        self.repository_id = repository_id
        self._paths: frozenset[str] | None = None
        self._blobs: dict[str, str | None] = {}

    async def list_paths(self) -> list[str]:
        if self._paths is None:
            raw = await self._git.list_paths(self.head_sha)
            self._paths = frozenset(
                p.replace("\\", "/") for p in raw if is_safe_repo_path(p.replace("\\", "/"))
            )
        return sorted(self._paths)

    async def get_blob(self, path: str) -> str | None:
        try:
            key = normalize_repo_path(path)
        except SandboxError:
            return None
        await self.list_paths()
        if self._paths is None or key not in self._paths:
            return None
        if key not in self._blobs:
            self._blobs[key] = await self._git.get_blob(self.head_sha, key)
        return self._blobs[key]


@dataclass
class ToolWorkspace:
    """单次 invoke 的执行上下文。"""

    repo_root: Path | None
    snapshot: ToolSnapshot
    diff_files: dict[str, ChangedFile] = field(default_factory=dict)
    symbol_index: SymbolIndex | None = None
    task_id: str | None = None
    run_id: str | None = None
