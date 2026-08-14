"""claimed → canonical 重定位（review/location.py，05 §3 / 07 §5 location 校验）。

模型只提供位置线索（claimed_path/claimed_start_line/claimed_end_line）；程序用
当前变更范围（diff 新增行号表）确认并重定位：
- claimed 行落在新增行（容忍少量漂移）→ canonical 锚定新增行，status=location_valid；
- 行不在新增行 / 路径不在变更文件 / 无行号 → body_only（正文结论仍有效，不进行内）；
- 仅程序可写 canonical 字段（幻觉防线，05 §4）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import DiffLineType
from ..domain.finding import FindingCandidate
from ..domain.models import ChangedFile

# 行号漂移容差：模型行号可能指向 hunk 上下文行，容忍 ±N 行内找最近新增行
LINE_DRIFT_TOLERANCE = 5

LOCATION_VALID = "location_valid"
BODY_ONLY = "body_only"


@dataclass(frozen=True)
class LocationResolution:
    """重定位结果（canonical 字段仅此处产生）。"""

    path: str | None
    start_line: int | None
    end_line: int | None
    status: str
    reason: str


def added_line_numbers(file: ChangedFile) -> set[int]:
    """该文件 diff 中全部新增行号（canonical 锚点集合）。"""
    added: set[int] = set()
    for hunk in file.hunks:
        for line in hunk.lines:
            if line.type is DiffLineType.ADDED and line.new_ln is not None:
                added.add(line.new_ln)
    return added


def _nearest(value: int, candidates: set[int], tolerance: int) -> int | None:
    """在容差内找最接近 value 的新增行；value 本身在集合内则原样返回。"""
    if value in candidates:
        return value
    best: int | None = None
    best_dist = tolerance + 1
    for c in candidates:
        dist = abs(c - value)
        if dist <= tolerance and dist < best_dist:
            best = c
            best_dist = dist
    return best


def resolve_location(
    candidate: FindingCandidate,
    file_map: dict[str, ChangedFile],
) -> LocationResolution:
    """claimed → canonical 重定位。"""
    path = candidate.claimed_path
    if not path:
        return LocationResolution(None, None, None, BODY_ONLY, "缺少 claimed_path")
    file = file_map.get(path)
    if file is None:
        return LocationResolution(None, None, None, BODY_ONLY, "claimed_path 不在本次变更文件")
    added = added_line_numbers(file)
    if not added:
        return LocationResolution(path, None, None, BODY_ONLY, "文件无新增行（纯删除/仅重命名）")

    start = candidate.claimed_start_line
    if start is None:
        return LocationResolution(path, None, None, BODY_ONLY, "缺少 claimed_start_line")
    anchor = _nearest(start, added, LINE_DRIFT_TOLERANCE)
    if anchor is None:
        return LocationResolution(
            path, None, None, BODY_ONLY,
            f"claimed 行 {start} 不在新增行（容差 {LINE_DRIFT_TOLERANCE}）",
        )

    end_anchor = anchor
    end = candidate.claimed_end_line
    if end is not None and end > anchor:
        near_end = _nearest(end, added, LINE_DRIFT_TOLERANCE)
        if near_end is not None:
            end_anchor = max(anchor, near_end)
    return LocationResolution(path, anchor, end_anchor, LOCATION_VALID, "ok")


__all__ = [
    "BODY_ONLY",
    "LINE_DRIFT_TOLERANCE",
    "LOCATION_VALID",
    "LocationResolution",
    "added_line_numbers",
    "resolve_location",
]
