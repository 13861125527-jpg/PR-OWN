"""tools 包 — V3 只读工具（V3-A：registry / sandbox / schema）。"""

from __future__ import annotations

from . import (
    find_files,
    find_references,
    finish_review,
    read_diff,
    read_file,
    search_code,
    submit_finding,
)
from .invoke import invoke, is_repeat
from .registry import ToolRegistry
from .snapshot import GitToolSnapshot, MemoryToolSnapshot, ToolWorkspace


def builtin_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(read_file.DEFINITION, read_file.ReadFileArgs, read_file.handle)
    registry.register(find_files.DEFINITION, find_files.FindFilesArgs, find_files.handle)
    registry.register(search_code.DEFINITION, search_code.SearchCodeArgs, search_code.handle)
    registry.register(
        find_references.DEFINITION, find_references.FindReferencesArgs, find_references.handle
    )
    registry.register(read_diff.DEFINITION, read_diff.ReadDiffArgs, read_diff.handle)
    registry.register(
        submit_finding.DEFINITION, submit_finding.SubmitFindingArgs, submit_finding.handle
    )
    registry.register(
        finish_review.DEFINITION, finish_review.FinishReviewArgs, finish_review.handle
    )
    return registry


__all__ = [
    "GitToolSnapshot",
    "MemoryToolSnapshot",
    "ToolRegistry",
    "ToolWorkspace",
    "builtin_registry",
    "invoke",
    "is_repeat",
]
