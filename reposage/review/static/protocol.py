"""静态分析器协议与内部结果（V2-C）。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from ...domain.finding import FindingCandidate
from ...domain.models import ChangedFile, CoverageItem
from ...domain.run import ReviewTask


@dataclass(frozen=True)
class AnalyzerDiagnostic:
    analyzer_id: str
    rule_id: str
    path: str
    start_line: int
    end_line: int | None = None
    message: str = ""
    severity_hint: str | None = None


@dataclass
class AnalyzerRunResult:
    candidates: list[FindingCandidate] = field(default_factory=list)
    diagnostics: list[AnalyzerDiagnostic] = field(default_factory=list)
    tasks: list[ReviewTask] = field(default_factory=list)
    coverage_items: list[CoverageItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str = "ok"  # ok | failed | cancelled | skipped | disabled
    elapsed_ms: int = 0


class StaticAnalyzer(Protocol):
    id: str

    async def analyze(
        self,
        files: list[ChangedFile],
        blobs: Mapping[str, str],
        *,
        timeout_s: float,
        select: list[str],
        denylist: list[str],
    ) -> AnalyzerRunResult: ...
