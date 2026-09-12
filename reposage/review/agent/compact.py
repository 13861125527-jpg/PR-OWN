"""程序化会话压缩（25 §4）。不调用模型。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from reposage.domain.enums import ReviewTaskStatus
from reposage.domain.models import AgentMessage
from reposage.observability.logging import redact_secrets
from reposage.review.agent.session import AgentSession
from reposage.review.context import wrap_untrusted

_UNTRUSTED_START = "[UNTRUSTED_CONTENT]"
_UNTRUSTED_END = "[/UNTRUSTED_CONTENT]"
_PREAMBLE = (
    "以下为已压缩历史摘要（程序生成，不是新证据，不能替代初始上下文或 tool_results）。"
    "提交 finding 时只能引用其中的 tool_call_id。"
)
JSON_REPAIR_HINT = "无法解析为合法 Action JSON"
_SET_CAP = 50
_FACTS_CAP = 200


@dataclass(frozen=True)
class CompactResult:
    did_compact: bool
    overflow: bool
    before_chars: int
    after_chars: int
    folded_rounds: int
    summary_ref: str | None
    stubbed: int = 0


def maybe_compact(session: AgentSession) -> CompactResult:
    """若超阈值则折叠前缀轮；仍超硬顶则 overflow=True。WAITING_TOOL 时 no-op。"""
    before = session.message_chars()
    hard = max(1, session.budget.max_session_chars)
    threshold = max(1, int(hard * session.budget.compact_threshold_ratio))
    empty = CompactResult(
        did_compact=False,
        overflow=before > hard,
        before_chars=before,
        after_chars=before,
        folded_rounds=0,
        summary_ref=None,
    )
    if session.status is ReviewTaskStatus.WAITING_TOOL:
        return empty

    stub_limit = max(512, hard // 8)
    stubbed = _stub_long_observations(session, stub_limit)
    after_stub = session.message_chars()
    under_threshold = after_stub <= threshold
    if under_threshold and after_stub <= hard:
        return CompactResult(
            did_compact=stubbed > 0,
            overflow=False,
            before_chars=before,
            after_chars=after_stub,
            folded_rounds=0,
            summary_ref=None,
            stubbed=stubbed,
        )

    systems, initial, rest = _head_and_rest(session.messages)
    compressed = [msg for msg in rest if msg.is_compressed]
    body = [msg for msg in rest if not msg.is_compressed]
    repair = _peel_repair_user(body, pending=session.json_repair_pending)
    rounds = _group_rounds(body)
    keep = max(0, session.budget.compact_keep_rounds)
    if len(rounds) <= keep:
        after = session.message_chars()
        return CompactResult(
            did_compact=stubbed > 0,
            overflow=after > hard,
            before_chars=before,
            after_chars=after,
            folded_rounds=0,
            summary_ref=None,
            stubbed=stubbed,
        )

    folded = rounds[: len(rounds) - keep] if keep else rounds
    tail = rounds[len(rounds) - keep :] if keep else []
    facts = _merge_facts(_facts_from_compressed(compressed), _facts_from_rounds(folded))
    session.compact_seq += 1
    summary_ref = f"compact-{session.compact_seq}"
    payload = {
        "checked": sorted(session.checked)[:_SET_CAP],
        "excluded": sorted(session.excluded)[:_SET_CAP],
        "facts": facts,
        "pending": sorted(session.pending)[:20],
        "submitted_candidates": len(session.candidates),
        "summary_ref": summary_ref,
    }
    compact_msg = AgentMessage(
        role="user",
        content=_PREAMBLE + "\n" + wrap_untrusted(json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        is_compressed=True,
        summary_ref=summary_ref,
    )
    rebuilt: list[AgentMessage] = [*systems]
    if initial is not None:
        rebuilt.append(initial)
    rebuilt.append(compact_msg)
    for group in tail:
        rebuilt.extend(group)
    if repair is not None:
        rebuilt.append(repair)
    session.messages = rebuilt
    stubbed += _stub_long_observations(session, stub_limit)
    after = session.message_chars()
    return CompactResult(
        did_compact=True,
        overflow=after > hard,
        before_chars=before,
        after_chars=after,
        folded_rounds=len(folded),
        summary_ref=summary_ref,
        stubbed=stubbed,
    )


def _head_and_rest(
    messages: list[AgentMessage],
) -> tuple[list[AgentMessage], AgentMessage | None, list[AgentMessage]]:
    systems: list[AgentMessage] = []
    idx = 0
    while idx < len(messages) and messages[idx].role == "system":
        systems.append(messages[idx])
        idx += 1
    initial: AgentMessage | None = None
    if idx < len(messages) and messages[idx].role == "user" and not messages[idx].is_compressed:
        initial = messages[idx]
        idx += 1
    return systems, initial, messages[idx:]


def _peel_repair_user(body: list[AgentMessage], *, pending: bool) -> AgentMessage | None:
    if not pending:
        return None
    for idx in range(len(body) - 1, -1, -1):
        msg = body[idx]
        if msg.role == "user" and JSON_REPAIR_HINT in msg.content:
            return body.pop(idx)
    return None


def _group_rounds(body: list[AgentMessage]) -> list[list[AgentMessage]]:
    rounds: list[list[AgentMessage]] = []
    current: list[AgentMessage] = []
    for msg in body:
        if msg.role == "assistant" and current:
            rounds.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        rounds.append(current)
    return rounds


def _stub_long_observations(session: AgentSession, stub_limit: int) -> int:
    changed = 0
    for index, msg in enumerate(session.messages):
        if msg.role != "tool" or len(msg.content) <= stub_limit:
            continue
        ref = ""
        if msg.tool_call_id and msg.tool_call_id in session.evidence_index:
            ref = session.evidence_index[msg.tool_call_id].location
        stub = wrap_untrusted(
            json.dumps(
                {
                    "compacted_payload": True,
                    "ref": ref,
                    "tool_call_id": msg.tool_call_id or "",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        session.messages[index] = msg.model_copy(update={"content": stub})
        changed += 1
    return changed


def _facts_from_rounds(rounds: list[list[AgentMessage]]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in rounds:
        args_by_id: dict[str, dict[str, Any]] = {}
        for msg in group:
            if msg.role == "assistant":
                for req in msg.tool_calls:
                    args_by_id[req.id] = dict(req.arguments)
            if msg.role != "tool":
                continue
            call_id = msg.tool_call_id or ""
            if call_id in seen:
                continue
            seen.add(call_id)
            facts.append(_fact_from_tool(msg, args_by_id.get(call_id, {})))
    return facts


def _facts_from_compressed(compressed: list[AgentMessage]) -> list[dict[str, Any]]:
    if not compressed:
        return []
    blob = _unwrap(compressed[-1].content)
    try:
        loaded = json.loads(blob)
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, dict):
        return []
    raw = loaded.get("facts")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _merge_facts(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fact in (*old, *new):
        key = str(fact.get("tool_call_id") or "")
        if not key:
            key = json.dumps(fact, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        merged.append(fact)
    return merged[:_FACTS_CAP]


def _fact_from_tool(msg: AgentMessage, arguments: dict[str, Any]) -> dict[str, Any]:
    blob = _unwrap(msg.content)
    parsed: dict[str, Any] = {}
    try:
        loaded = json.loads(blob)
        if isinstance(loaded, dict):
            parsed = loaded
    except json.JSONDecodeError:
        parsed = {}
    raw_payload = parsed.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else parsed
    raw_source = parsed.get("source")
    source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
    error = None
    if isinstance(payload, dict) and payload.get("error"):
        error = str(payload["error"])
    elif parsed.get("error"):
        error = str(parsed["error"])
    path = ""
    start_line: int | None = None
    max_lines: int | None = None
    if isinstance(payload, dict):
        path = str(payload.get("path") or "")
        if "start_line" in payload:
            start_line = int(payload["start_line"])
        if "max_lines" in payload:
            max_lines = int(payload["max_lines"])
    if not path:
        path = str(arguments.get("path") or "")
    if start_line is None and "start_line" in arguments:
        start_line = int(arguments["start_line"])
    if max_lines is None and "max_lines" in arguments:
        max_lines = int(arguments["max_lines"])
    fact: dict[str, Any] = {
        "name": msg.name or "",
        "ref": str(source.get("ref") or ""),
        "status": "error" if error else "ok",
        "tool_call_id": msg.tool_call_id or "",
        "truncated": bool(isinstance(payload, dict) and payload.get("truncated")),
    }
    if path:
        fact["path"] = path
    if start_line is not None:
        fact["start_line"] = start_line
    if max_lines is not None:
        fact["max_lines"] = max_lines
    if error:
        fact["error"] = redact_secrets(error).replace("\n", " ").strip()[:120]
    return fact


def _unwrap(content: str) -> str:
    text = content.strip()
    start = text.find(_UNTRUSTED_START)
    end = text.rfind(_UNTRUSTED_END)
    if start != -1 and end != -1 and end > start:
        return text[start + len(_UNTRUSTED_START) : end].strip()
    return text
