"""LLM 候选边界清洗（V2-C DP-7）。

source_kind / rule_id / analyzer_id 仅程序写入；Strategy 返回前必须清零模型夹带值。
"""

from __future__ import annotations

from ..domain.finding import FindingCandidate


def sanitize_llm_candidate(cand: FindingCandidate) -> FindingCandidate:
    """强制丢掉模型不可写的来源字段；不改 role_id（由调用方程序盖戳）。"""
    return cand.model_copy(
        update={"source_kind": None, "rule_id": None, "analyzer_id": None}
    )


def prepare_llm_candidate(
    cand: FindingCandidate,
    *,
    file_path: str,
    role_id: str | None = None,
) -> FindingCandidate:
    """Strategy 边界：清洗来源字段，并按既有规则补 claimed_path / role_id。"""
    updates: dict[str, object] = {
        "source_kind": None,
        "rule_id": None,
        "analyzer_id": None,
    }
    if role_id is not None:
        updates["role_id"] = role_id
    if cand.claimed_path is None and not cand.is_outside_diff:
        updates["claimed_path"] = file_path
    return cand.model_copy(update=updates)
