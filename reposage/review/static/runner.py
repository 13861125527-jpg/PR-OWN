"""ReviewService 侧静态分析编排（V2-C）。"""

from __future__ import annotations

import asyncio
import time

from ...config.settings import StaticConfig
from ...domain.enums import (
    ChangedFileStatus,
    CoverageReason,
    ReviewTaskKind,
    ReviewTaskStatus,
    StageName,
)
from ...domain.models import ChangedFile, CoverageItem
from ...domain.protocols import GitSnapshotProvider
from ...domain.run import ReviewTask
from ..symbols.snapshot import HeadSnapshot
from .convert import convert_diagnostics
from .protocol import AnalyzerRunResult, StaticAnalyzer
from .ruff import RuffAnalyzer

_MAX_BLOB_BYTES = 1_048_576


def is_python_target(file: ChangedFile) -> bool:
    if file.status is ChangedFileStatus.DELETED:
        return False
    if file.is_binary or file.is_generated:
        return False
    lang = (file.language or "").lower()
    if lang == "python":
        return True
    return file.path.replace("\\", "/").endswith(".py")


async def collect_static_blobs(
    files: list[ChangedFile],
    *,
    head_sha: str,
    provider: GitSnapshotProvider | None,
    snapshot: HeadSnapshot | None,
) -> tuple[dict[str, str], list[CoverageItem], list[str]]:
    """读取 changed Python 文件的 head blob；不从 hunk 拼文件。"""
    coverage: list[CoverageItem] = []
    warnings: list[str] = []
    blobs: dict[str, str] = {}
    python_files = [f for f in files if is_python_target(f)]
    if not python_files:
        return blobs, coverage, warnings
    if provider is None and snapshot is None:
        warnings.append("static capability miss: GitSnapshotProvider")
        coverage.append(
            CoverageItem(
                target="static",
                reason=CoverageReason.TRUNCATED,
                stage=StageName.REVIEW,
                detail="capability miss: GitSnapshotProvider",
            )
        )
        return blobs, coverage, warnings
    for file in python_files:
        path = file.path.replace("\\", "/")
        text = snapshot.get(path) if snapshot is not None else None
        if text is None and provider is not None:
            text = await provider.get_blob(head_sha, path)
        if text is None:
            coverage.append(
                CoverageItem(
                    target=path,
                    reason=CoverageReason.TRUNCATED,
                    stage=StageName.REVIEW,
                    detail="static blob missing",
                )
            )
            continue
        if len(text.encode("utf-8")) > _MAX_BLOB_BYTES:
            coverage.append(
                CoverageItem(
                    target=path,
                    reason=CoverageReason.SKIPPED_SIZE,
                    stage=StageName.REVIEW,
                    detail="static blob too large",
                )
            )
            continue
        blobs[path] = text
        coverage.append(
            CoverageItem(
                target=path,
                reason=CoverageReason.COVERED,
                stage=StageName.REVIEW,
                detail="static analyzed",
            )
        )
    return blobs, coverage, warnings


async def run_static_analysis(
    *,
    run_id: str,
    files: list[ChangedFile],
    file_map: dict[str, ChangedFile],
    head_sha: str,
    settings: StaticConfig,
    provider: GitSnapshotProvider | None,
    snapshot: HeadSnapshot | None,
    analyzer: StaticAnalyzer | None = None,
) -> AnalyzerRunResult:
    """跑配置中的分析器，转换候选。CancelledError 不吞。"""
    t0 = time.perf_counter()
    if not settings.enabled:
        return AnalyzerRunResult(status="disabled")
    impl: StaticAnalyzer = analyzer if analyzer is not None else RuffAnalyzer()
    blobs, coverage, warnings = await collect_static_blobs(
        files, head_sha=head_sha, provider=provider, snapshot=snapshot
    )
    python_files = [f for f in files if is_python_target(f)]
    task = ReviewTask(
        task_id=f"{run_id}:static:{impl.id}",
        run_id=run_id,
        kind=ReviewTaskKind.STATIC_ANALYZE,
        target=impl.id,
        status=ReviewTaskStatus.RUNNING,
    )
    result = AnalyzerRunResult(coverage_items=coverage, warnings=warnings, status="ok")
    try:
        return await _run_static_body(
            result,
            task,
            impl,
            python_files,
            blobs,
            warnings,
            settings,
            file_map,
        )
    finally:
        result.elapsed_ms = int((time.perf_counter() - t0) * 1000)


async def _run_static_body(
    result: AnalyzerRunResult,
    task: ReviewTask,
    impl: StaticAnalyzer,
    python_files: list[ChangedFile],
    blobs: dict[str, str],
    warnings: list[str],
    settings: StaticConfig,
    file_map: dict[str, ChangedFile],
) -> AnalyzerRunResult:
    cap_miss = any("capability miss" in w for w in warnings) and not blobs
    if cap_miss:
        task.status = ReviewTaskStatus.FAILED
        task.error = "capability miss: GitSnapshotProvider"
        result.tasks = [task]
        result.status = "failed"
        return result
    try:
        raw = await asyncio.wait_for(
            impl.analyze(
                python_files,
                blobs,
                timeout_s=settings.timeout_seconds,
                select=list(settings.rule_subsets),
                denylist=list(settings.denylist),
            ),
            timeout=settings.timeout_seconds,
        )
    except asyncio.CancelledError:
        task.status = ReviewTaskStatus.CANCELLED
        task.error = "cancelled"
        result.tasks = [task]
        result.status = "cancelled"
        raise
    except FileNotFoundError:
        task.status = ReviewTaskStatus.FAILED
        task.error = "capability miss: ruff"
        result.tasks = [task]
        result.status = "failed"
        result.warnings.append("static capability miss: ruff")
        result.coverage_items.append(
            CoverageItem(
                target="static",
                reason=CoverageReason.TRUNCATED,
                stage=StageName.REVIEW,
                detail="capability miss: ruff",
            )
        )
        return result
    except TimeoutError:
        task.status = ReviewTaskStatus.FAILED
        task.error = "ruff timeout"
        result.tasks = [task]
        result.status = "failed"
        result.warnings.append("static timeout")
        result.coverage_items.append(
            CoverageItem(
                target="static",
                reason=CoverageReason.TRUNCATED,
                stage=StageName.REVIEW,
                detail="ruff timeout",
            )
        )
        return result
    if raw.status == "failed" or any("capability miss" in w for w in raw.warnings):
        task.status = ReviewTaskStatus.FAILED
        task.error = (raw.warnings[0] if raw.warnings else "static analyzer failed")[:300]
        result.status = "failed"
        result.warnings.extend(raw.warnings)
        if any("capability miss" in w for w in raw.warnings):
            result.coverage_items.append(
                CoverageItem(
                    target="static",
                    reason=CoverageReason.TRUNCATED,
                    stage=StageName.REVIEW,
                    detail=raw.warnings[0],
                )
            )
        result.tasks = [task]
        return result
    candidates, skipped = convert_diagnostics(raw.diagnostics, file_map)
    result.diagnostics = list(raw.diagnostics)
    result.candidates = candidates
    result.coverage_items.extend(skipped)
    result.warnings.extend(raw.warnings)
    task.status = ReviewTaskStatus.COMPLETED
    result.tasks = [task]
    result.status = "ok"
    return result


def empty_static_result() -> AnalyzerRunResult:
    return AnalyzerRunResult(status="disabled")
