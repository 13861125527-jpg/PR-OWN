"""脚本化静态分析器（单测 / 对照）。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from ...domain.models import ChangedFile
from .protocol import AnalyzerDiagnostic, AnalyzerRunResult


class FakeStaticAnalyzer:
    id = "ruff"

    def __init__(
        self,
        diagnostics: list[AnalyzerDiagnostic] | None = None,
        *,
        fail: bool = False,
        hang: bool = False,
        missing: bool = False,
    ) -> None:
        self.diagnostics = list(diagnostics or [])
        self.fail = fail
        self.hang = hang
        self.missing = missing

    async def analyze(
        self,
        files: list[ChangedFile],
        blobs: Mapping[str, str],
        *,
        timeout_s: float,
        select: list[str],
        denylist: list[str],
    ) -> AnalyzerRunResult:
        del files, blobs, select
        if self.missing:
            raise FileNotFoundError("ruff")
        if self.hang:
            await asyncio.sleep(max(timeout_s + 5.0, 5.0))
        if self.fail:
            return AnalyzerRunResult(status="failed", warnings=["fake analyzer failed"])
        deny = {code.upper() for code in denylist}
        kept = [d for d in self.diagnostics if d.rule_id.upper() not in deny]
        return AnalyzerRunResult(diagnostics=kept, status="ok")
