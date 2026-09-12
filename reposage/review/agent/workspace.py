"""每次 review 的工具 Workspace 工厂（24 §8.3 / DP-23）。"""

from __future__ import annotations

from pathlib import Path

from reposage.domain.models import ChangedFile
from reposage.review.symbols.extract import SymbolIndex
from reposage.tools.snapshot import ToolSnapshot, ToolWorkspace


class AgentWorkspaceFactory:
    """锁定 snapshot + 结构化 file_map；不解析 prompt。"""

    def __init__(
        self,
        snapshot: ToolSnapshot,
        diff_files: dict[str, ChangedFile],
        *,
        sandbox_root: Path | None,
        symbol_index: SymbolIndex | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.diff_files = dict(diff_files)
        self.sandbox_root = sandbox_root
        self.symbol_index = symbol_index

    def build(self, file_path: str, task_id: str, run_id: str) -> ToolWorkspace:
        del file_path
        return ToolWorkspace(
            repo_root=self.sandbox_root,
            snapshot=self.snapshot,
            diff_files=self.diff_files,
            symbol_index=self.symbol_index,
            task_id=task_id,
            run_id=run_id,
        )
