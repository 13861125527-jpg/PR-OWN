"""V3-D 跨文件 V2 vs V3 对照（26 §6 / T3–T4）。脚本化 Fake，无真实模型。"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import statistics
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from reposage.config.settings import Settings
from reposage.domain.diff import parse_unified_diff
from reposage.domain.enums import FindingCategory, FindingStatus, ReviewTaskStatus, Severity
from reposage.domain.finding import Finding, FindingCandidate
from reposage.domain.models import AgentToolRequest, ModelUsage
from reposage.domain.protocols import ModelResponse
from reposage.evals.agent_ops import (
    failure_rate,
    groundedness,
    repeat_rate,
    tool_effectiveness,
    tool_groundedness,
)
from reposage.evals.dataset import EvalDataset, EvalSample
from reposage.evals.metrics import Metrics, compute_metrics
from reposage.evals.v2a_compare import _avg, _avg_position, _percentile
from reposage.observability.logging import StructuredLogger
from reposage.providers.git.fake import FakeGitProvider
from reposage.providers.llm.fake import FakeLLMProvider
from reposage.providers.llm.openai_compat import _action_json_response
from reposage.review.agent import reviewer as agent_reviewer
from reposage.review.agent.session import AgentSession
from reposage.review.location import added_line_numbers
from reposage.review.service import ReviewService
from reposage.storage.sqlite import SqliteStorage

_DEFAULT_DATASET = "reposage/evals/datasets/v3d_cross_file.yaml"
_DEFAULT_JSON = "docs/evidence/v3-d-compare.json"
_DEFAULT_MD = "docs/evidence/v3-d-compare.md"
_RECOMMENDATION = "keep_agent_experimental"
_NOTE_KEYS = ("l3_expected", "anchor", "evidence_path", "quality")
_L3_VALUES = frozenset({"hit", "miss", "n/a"})
_QUALITY_VALUES = frozenset({"include", "exclude"})


def _note_fields(notes: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw in notes.replace(": ", "=").split():
        if ":" in raw and "=" not in raw:
            raw = raw.replace(":", "=", 1)
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key, value = key.strip(), value.strip().strip(",")
        if key:
            fields[key] = value
    return fields


def _changed_paths(sample: EvalSample) -> set[str]:
    paths = set(sample.base_files) | set(sample.head_files)
    return {path for path in paths if sample.base_files.get(path) != sample.head_files.get(path)}


def validate_dataset(ds: EvalDataset) -> dict[str, Any]:
    """YAML notes 为源；anchor 不得出现在变更文件或 title/description/expected.note。"""
    errors: list[dict[str, str]] = []
    for sample in ds.samples:
        fields = _note_fields(sample.notes)
        missing = [key for key in _NOTE_KEYS if not fields.get(key)]
        if missing:
            errors.append(
                {"id": sample.id, "code": "missing_field", "detail": ",".join(missing)}
            )
            continue
        if fields["l3_expected"] not in _L3_VALUES:
            errors.append(
                {
                    "id": sample.id,
                    "code": "bad_l3_expected",
                    "detail": fields["l3_expected"],
                }
            )
        if fields["quality"] not in _QUALITY_VALUES:
            errors.append({"id": sample.id, "code": "bad_quality", "detail": fields["quality"]})
        path = fields["evidence_path"]
        blob = sample.head_files.get(path)
        if blob is None:
            errors.append({"id": sample.id, "code": "evidence_path_missing", "detail": path})
        elif fields["anchor"] not in blob:
            errors.append(
                {"id": sample.id, "code": "anchor_missing_from_evidence", "detail": path}
            )
        meta_leaks = [
            name
            for name, text in (
                ("pr_title", sample.pr_title),
                ("pr_description", sample.pr_description),
            )
            if fields["anchor"] in text
        ]
        if any(fields["anchor"] in item.note for item in sample.expected):
            meta_leaks.append("expected.note")
        if meta_leaks:
            errors.append(
                {"id": sample.id, "code": "anchor_leak", "detail": ",".join(meta_leaks)}
            )
        if fields["l3_expected"] != "miss":
            continue
        leaked = sorted(
            p
            for p in _changed_paths(sample)
            if fields["anchor"] in (sample.head_files.get(p) or "")
            or fields["anchor"] in (sample.base_files.get(p) or "")
        )
        if leaked:
            errors.append(
                {"id": sample.id, "code": "anchor_leak", "detail": ",".join(leaked)}
            )
        if path in _changed_paths(sample):
            errors.append(
                {"id": sample.id, "code": "anchor_leak", "detail": f"evidence_path={path}"}
            )
    status = "ok" if not errors else "dataset_invalid"
    return {"status": status, "errors": errors}


def _quality_excluded(ds: EvalDataset) -> list[str]:
    return [
        sample.id
        for sample in ds.samples
        if _note_fields(sample.notes).get("quality") == "exclude"
    ]


def _read_args(sample: EvalSample) -> dict[str, Any]:
    path = _note_fields(sample.notes).get("evidence_path") or "src/util.py"
    return {"path": path, "start_line": 1, "max_lines": 40}


def _resp(name: str, args: dict[str, Any], call_id: str) -> ModelResponse:
    action = name if name in {"submit_finding", "finish_review"} else "tool_call"
    req = AgentToolRequest(id=call_id, name=name, arguments=args)
    return ModelResponse(action=action, data={"name": name, "arguments": args}, tool_requests=[req])


def _parse_error(msg: str = "bad json") -> ModelResponse:
    return ModelResponse(action=None, data={"parse_error": msg})


def _via_action_json(script: list[ModelResponse]) -> list[ModelResponse]:
    """把 Fake 脚本经真实 Action JSON 解析；submit 的 tool id 改成解析器分配的 id。"""
    usage = ModelUsage(model="fake", role="eval", input_tokens=1, output_tokens=1, cost_usd=0.0)
    id_map: dict[str, str] = {}
    out: list[ModelResponse] = []
    for resp in script:
        if resp.action is None and isinstance(resp.data, dict) and "parse_error" in resp.data:
            parsed = _action_json_response("{not json", usage)
            parsed.usage = None
            out.append(parsed)
            continue
        req = resp.tool_requests[0] if resp.tool_requests else None
        data = resp.data or {}
        name = req.name if req is not None else str(data.get("name") or "")
        args = dict(req.arguments) if req is not None else {}
        raw_args = data.get("arguments")
        if not args and isinstance(raw_args, dict):
            args = dict(raw_args)
        action = resp.action or "tool_call"
        if action == "submit_finding":
            cited = args.get("evidence_tool_call_ids")
            if isinstance(cited, list) and cited:
                args = dict(args)
                args["evidence_tool_call_ids"] = [id_map.get(str(old), str(old)) for old in cited]
        text = json.dumps({"action": action, "name": name, "args": args}, ensure_ascii=False)
        parsed = _action_json_response(text, usage)
        parsed.usage = None
        if action == "tool_call" and req is not None and parsed.tool_requests:
            id_map[req.id] = parsed.tool_requests[0].id
        out.append(parsed)
    return out


def _submit_args(
    sample: EvalSample,
    evidence_id: str | None,
    *,
    confidence: float,
) -> dict[str, Any]:
    expected = sample.expected[0]
    ids = [evidence_id] if evidence_id else []
    return {
        "title": "eval of untrusted input",
        "severity": "high",
        "confidence": confidence,
        "category": expected.category,
        "claimed_path": expected.path,
        "claimed_start_line": expected.line,
        "trigger_condition": "eval",
        "explanation": "scripted v3",
        "suggestion": "avoid eval",
        "evidence_tool_call_ids": ids,
    }


def _v3_script(sample: EvalSample, *, protocol: str) -> list[ModelResponse]:
    finish = _resp("finish_review", {"reason": "done"}, f"{sample.id}-fin")
    read = _read_args(sample)
    fields = _note_fields(sample.notes)
    if sample.id == "xf-negative":
        script = [_resp("read_file", dict(read), f"{sample.id}-read"), finish]
    elif sample.id == "xf-ungrounded":
        script = [
            _resp("read_file", dict(read), f"{sample.id}-read"),
            _resp(
                "submit_finding",
                _submit_args(sample, None, confidence=0.9),
                f"{sample.id}-sub",
            ),
            finish,
        ]
    elif sample.id == "xf-grounded":
        read_id = f"{sample.id}-refs"
        script = [
            _resp("find_references", {"symbol": "helper", "scope": "repo"}, read_id),
            _resp(
                "submit_finding",
                _submit_args(sample, read_id, confidence=1.0),
                f"{sample.id}-sub",
            ),
            finish,
        ]
    elif sample.id == "xf-l3-miss-search":
        read_id = f"{sample.id}-search"
        script = [
            _resp("search_code", {"query": fields.get("anchor") or ""}, read_id),
            _resp(
                "submit_finding",
                _submit_args(sample, read_id, confidence=1.0),
                f"{sample.id}-sub",
            ),
            finish,
        ]
    elif sample.id == "xf-spam-tools":
        read_id = f"{sample.id}-r0"
        spam = [_resp("read_file", dict(read), f"{sample.id}-r{i}") for i in range(4)]
        script = [
            *spam,
            _resp(
                "submit_finding",
                _submit_args(sample, read_id, confidence=1.0),
                f"{sample.id}-sub",
            ),
            finish,
        ]
    else:
        read_id = f"{sample.id}-read"
        script = [
            _resp("read_file", dict(read), read_id),
            _resp(
                "submit_finding",
                _submit_args(sample, read_id, confidence=1.0),
                f"{sample.id}-sub",
            ),
            finish,
        ]
    if protocol == "action_json":
        if sample.id == "xf-l3-miss":
            script = [_parse_error("v3d-ab"), *script]
        return _via_action_json(script)
    return script


def _v2_candidates(sample: EvalSample) -> list[FindingCandidate]:
    if not sample.expected:
        return []
    expected = sample.expected[0]
    return [
        FindingCandidate(
            title="scripted eval",
            severity=Severity.HIGH,
            confidence=0.9,
            category=FindingCategory(expected.category),
            claimed_path=expected.path,
            claimed_start_line=expected.line,
            trigger_condition="eval",
            explanation="scripted v2",
            suggestion="avoid eval",
        )
    ]


def _v2_settings() -> Settings:
    """DP-2：V2 臂就是仓库默认管道，不另关 L3、不另开开关。"""
    return Settings()


def _v3_settings(protocol: str) -> Settings:
    """DP-3：只打开 agentic + enabled + 协议；其余沿用 Settings() 默认。"""
    return Settings.model_validate(
        {
            "review": {"strategy": "agentic"},
            "agent": {"enabled": True, "tool_protocol": protocol},
        }
    )


def _accepted(findings: list[Finding]) -> list[Finding]:
    return [item for item in findings if item.status is FindingStatus.ACCEPTED]


def _defect_hit(sample: EvalSample, metrics: Metrics) -> int:
    if not sample.expected:
        return 0
    return int(metrics.recall >= 1.0)


def _finding_tool_ids(findings: list[Finding]) -> list[str]:
    ids: list[str] = []
    for finding in findings:
        for ev in finding.evidence:
            if ev.tool_call_id and ev.tool_call_id not in ids:
                ids.append(ev.tool_call_id)
    return ids


def _peel_tool_payload(content: str) -> Any:
    raw = content
    marker = "[UNTRUSTED_CONTENT]"
    if marker in raw:
        raw = raw.split(marker, 1)[1].split("[/UNTRUSTED_CONTENT]", 1)[0]
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        return raw.strip()


def _payload_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return "\n".join(_payload_text(value) for value in payload.values())
    if isinstance(payload, list):
        return "\n".join(_payload_text(item) for item in payload)
    return str(payload)


def _evidence_chain(
    sessions: list[AgentSession],
    *,
    cited_ids: list[str],
    evidence_path: str,
    anchor: str,
) -> tuple[bool, bool]:
    cited = set(cited_ids)
    if not cited:
        return False, False
    path_ok = False
    anchor_ok = False
    for session in sessions:
        for msg in session.messages:
            if msg.role != "tool" or msg.tool_call_id not in cited:
                continue
            payload = _peel_tool_payload(msg.content)
            blob = _payload_text(payload)
            if evidence_path and evidence_path in blob:
                path_ok = True
            if anchor and anchor in blob:
                anchor_ok = True
        for call in session.calls:
            if call.tool_call_id not in cited:
                continue
            if call.arguments.get("path") == evidence_path:
                path_ok = True
    return path_ok, anchor_ok


def _repeat_observations(session: AgentSession) -> int:
    n = 0
    for msg in session.messages:
        if msg.role != "tool":
            continue
        payload = _peel_tool_payload(msg.content)
        if isinstance(payload, dict) and payload.get("error") == "repeat":
            n += 1
    return n


def _diff_locations(sample: EvalSample) -> set[tuple[str, int]]:
    git = FakeGitProvider()
    git.add_snapshot("base", sample.base_files)
    git.add_snapshot("head", sample.head_files)
    diff_text = asyncio.run(git.get_diff("base", "head"))
    out: set[tuple[str, int]] = set()
    for changed in parse_unified_diff(diff_text):
        for line in added_line_numbers(changed):
            out.add((changed.path, line))
    return out


@contextmanager
def _capture_sessions() -> Iterator[list[AgentSession]]:
    captured: list[AgentSession] = []
    orig = agent_reviewer.run_agent_loop  # type: ignore[attr-defined]

    async def wrapped(session: AgentSession, *args: Any, **kwargs: Any) -> Any:
        captured.append(session)
        return await orig(session, *args, **kwargs)

    agent_reviewer.run_agent_loop = wrapped  # type: ignore[attr-defined]
    try:
        yield captured
    finally:
        agent_reviewer.run_agent_loop = orig  # type: ignore[attr-defined]


def _mount_sample(git: FakeGitProvider, sample: EvalSample, number: int) -> None:
    git.add_snapshot("base", sample.base_files)
    git.add_snapshot("head", sample.head_files)
    git.add_pr(number, base="base", head="head", title=sample.pr_title)


def _store_tool_ids(store: SqliteStorage) -> set[str]:
    rows = store._query("SELECT tool_call_id FROM tool_calls")  # noqa: SLF001
    return {str(row["tool_call_id"]) for row in rows}


def _store_tool_count(store: SqliteStorage) -> int:
    return int(store._query("SELECT COUNT(*) AS n FROM tool_calls")[0]["n"])  # noqa: SLF001


async def _review(
    sample: EvalSample,
    *,
    settings: Settings,
    llm: FakeLLMProvider,
    pr_number: int,
) -> tuple[list[Finding], FakeLLMProvider, SqliteStorage]:
    git = FakeGitProvider()
    _mount_sample(git, sample, pr_number)
    store = SqliteStorage(":memory:")
    service = ReviewService(
        git,
        llm,
        store,
        settings=settings,
        logger=StructuredLogger(sink=io.StringIO()),
    )
    _run, findings = await service.review(str(pr_number))
    del _run
    return findings, llm, store


def _run_v2(sample: EvalSample, pr_number: int) -> dict[str, Any]:
    fields = _note_fields(sample.notes)
    llm = FakeLLMProvider(
        default_findings=_v2_candidates(sample),
        require_content=fields.get("anchor"),
    )
    t0 = time.perf_counter()
    findings, llm, store = asyncio.run(
        _review(sample, settings=_v2_settings(), llm=llm, pr_number=pr_number)
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    accepted = _accepted(findings)
    metrics = compute_metrics(sample.expected, accepted, sample_kind=sample.kind)
    metrics.details["n_expected"] = len(sample.expected)
    metrics.details["sample_kind"] = sample.kind
    return {
        "metrics": metrics,
        "elapsed_ms": elapsed_ms,
        "model_calls": len(llm.calls),
        "input_tokens": llm.input_tokens * len(llm.calls),
        "output_tokens": llm.output_tokens * len(llm.calls),
        "tool_calls": _store_tool_count(store),
        "cost_usd": round(llm.cost_usd * len(llm.calls), 6),
        "accepted": len(accepted),
        "hit": _defect_hit(sample, metrics),
    }


def _run_v3(sample: EvalSample, pr_number: int, *, protocol: str) -> dict[str, Any]:
    llm = FakeLLMProvider(tool_script=_v3_script(sample, protocol=protocol))
    locations = _diff_locations(sample)
    t0 = time.perf_counter()
    with _capture_sessions() as sessions:
        findings, llm, store = asyncio.run(
            _review(sample, settings=_v3_settings(protocol), llm=llm, pr_number=pr_number)
        )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    accepted = _accepted(findings)
    metrics = compute_metrics(sample.expected, accepted, sample_kind=sample.kind)
    metrics.details["n_expected"] = len(sample.expected)
    metrics.details["sample_kind"] = sample.kind
    store_ids = _store_tool_ids(store)
    session_ids = {key for session in sessions for key in session.evidence_index}
    tool_ids = store_ids | session_ids
    attempts = sum(session.tool_attempts for session in sessions)
    successful = sum(session.successful_tools for session in sessions)
    repeats = sum(_repeat_observations(session) for session in sessions)
    agent_tasks = [session.task for session in sessions]
    failed = sum(
        1
        for session in sessions
        if session.status in {ReviewTaskStatus.PARTIAL, ReviewTaskStatus.FAILED}
    )
    ground = groundedness(accepted, tool_call_ids=tool_ids, diff_locations=locations)
    tool_ground = tool_groundedness(accepted, tool_call_ids=tool_ids)
    fields = _note_fields(sample.notes)
    path_ok, anchor_ok = _evidence_chain(
        sessions,
        cited_ids=_finding_tool_ids(accepted),
        evidence_path=fields.get("evidence_path") or "",
        anchor=fields.get("anchor") or "",
    )
    return {
        "metrics": metrics,
        "elapsed_ms": elapsed_ms,
        "model_calls": len(llm.calls),
        "input_tokens": llm.input_tokens * len(llm.calls),
        "output_tokens": llm.output_tokens * len(llm.calls),
        "tool_calls": _store_tool_count(store),
        "cost_usd": round(llm.cost_usd * len(llm.calls), 6),
        "accepted": len(accepted),
        "hit": _defect_hit(sample, metrics),
        "tool_call_ids": _finding_tool_ids(accepted),
        "needs_evidence": sum(1 for item in accepted if item.needs_evidence),
        "successful_tools": successful,
        "tool_attempts": attempts,
        "repeat_count": repeats,
        "effectiveness": tool_effectiveness(successful_tools=successful, tool_attempts=attempts),
        "repeat_rate": repeat_rate(repeat_count=repeats, tool_attempts=attempts),
        "groundedness": ground,
        "tool_groundedness": tool_ground,
        "agent_tasks": len(agent_tasks),
        "failed_or_partial": failed,
        "stop_reasons": [session.stop_reason for session in sessions],
        "json_repair_used": any(session.json_repair_used for session in sessions),
        "completed": all(session.status is ReviewTaskStatus.COMPLETED for session in sessions)
        if sessions
        else False,
        "ids_in_db": all(item in store_ids for item in _finding_tool_ids(accepted)),
        "evidence_path_ok": path_ok,
        "anchor_observed": anchor_ok,
    }


def _check_default_agent_off(sample: EvalSample) -> dict[str, Any]:
    git = FakeGitProvider()
    _mount_sample(git, sample, 99)
    store = SqliteStorage(":memory:")
    settings = Settings()
    service = ReviewService(
        git,
        FakeLLMProvider(default_findings=[]),
        store,
        settings=settings,
        logger=StructuredLogger(sink=io.StringIO()),
    )
    asyncio.run(service.review("99"))
    return {
        "enabled": settings.agent.enabled,
        "strategy": settings.review.strategy,
        "tool_calls": _store_tool_count(store),
        "tool_protocol": settings.agent.tool_protocol,
        "user_version": int(store._query("PRAGMA user_version")[0][0]),  # noqa: SLF001
    }


def run_compare(dataset_path: str | Path = _DEFAULT_DATASET, *, repeats: int = 3) -> dict[str, Any]:
    ds = EvalDataset.load_yaml(Path(dataset_path))
    validation = validate_dataset(ds)
    excluded = _quality_excluded(ds)
    v2_rows: list[dict[str, Any]] = []
    v3_rows: list[dict[str, Any]] = []
    ab_rows: list[dict[str, Any]] = []
    v2_walls: list[float] = []
    v3_walls: list[float] = []
    v2_ms = 0.0
    v3_ms = 0.0
    for repeat in range(repeats):
        pass_v2: list[dict[str, Any]] = []
        t_v2 = time.perf_counter()
        for index, sample in enumerate(ds.samples, start=1):
            pass_v2.append(_run_v2(sample, index + repeat * 1000))
        v2_walls.append((time.perf_counter() - t_v2) * 1000)
        pass_v3: list[dict[str, Any]] = []
        t_v3 = time.perf_counter()
        for index, sample in enumerate(ds.samples, start=1):
            pass_v3.append(_run_v3(sample, 100 + index + repeat * 1000, protocol="native"))
        v3_walls.append((time.perf_counter() - t_v3) * 1000)
        if not v2_rows:
            v2_rows, v3_rows = pass_v2, pass_v3
            v2_ms = sum(row["elapsed_ms"] for row in v2_rows)
            v3_ms = sum(row["elapsed_ms"] for row in v3_rows)
    for index, sample in enumerate(ds.samples, start=1):
        native = v3_rows[index - 1]
        action = _run_v3(sample, 200 + index, protocol="action_json")
        ab_rows.append(
            {
                "id": sample.id,
                "native_completed": native["completed"],
                "action_json_completed": action["completed"],
                "native_calls": native["model_calls"],
                "action_json_calls": action["model_calls"],
                "native_hit": native["hit"],
                "action_json_hit": action["hit"],
                "native_tool_call_ids": native["tool_call_ids"],
                "action_json_tool_call_ids": action["tool_call_ids"],
                "native_tool_groundedness": native["tool_groundedness"],
                "action_json_tool_groundedness": action["tool_groundedness"],
                "native_json_repair": native["json_repair_used"],
                "action_json_json_repair": action["json_repair_used"],
                "native_ids_in_db": native["ids_in_db"],
                "action_json_ids_in_db": action["ids_in_db"],
            }
        )
    default_agent = _check_default_agent_off(ds.samples[0])
    v1_demo = EvalDataset.load_yaml(Path("reposage/evals/datasets/v1_demo.yaml"))
    v1_demo_default = _check_default_agent_off(v1_demo.samples[0])
    v1_demo_default["dataset"] = "reposage/evals/datasets/v1_demo.yaml"
    v1_demo_default["sample_id"] = v1_demo.samples[0].id
    quality_pairs = [
        (v2, v3)
        for sample, v2, v3 in zip(ds.samples, v2_rows, v3_rows, strict=True)
        if _note_fields(sample.notes).get("quality") != "exclude"
    ]
    v2_metrics = [pair[0]["metrics"] for pair in quality_pairs]
    v3_metrics = [pair[1]["metrics"] for pair in quality_pairs]
    v3_success = sum(row["successful_tools"] for row in v3_rows)
    v3_attempts = sum(row["tool_attempts"] for row in v3_rows)
    v3_repeats = sum(row["repeat_count"] for row in v3_rows)
    v3_failed = sum(row["failed_or_partial"] for row in v3_rows)
    v3_tasks = sum(row["agent_tasks"] for row in v3_rows)
    grounds = [row["groundedness"] for row in v3_rows if row["groundedness"] is not None]
    tool_grounds = [
        row["tool_groundedness"] for row in v3_rows if row["tool_groundedness"] is not None
    ]
    per_sample = []
    for sample, v2, v3 in zip(ds.samples, v2_rows, v3_rows, strict=True):
        per_sample.append(
            {
                "id": sample.id,
                "l3_expected": _note_fields(sample.notes).get("l3_expected") or "n/a",
                "v2_hit": v2["hit"],
                "v3_hit": v3["hit"],
                "v3_tool_call_ids": v3["tool_call_ids"],
                "v3_effectiveness": round(v3["effectiveness"], 3),
                "v3_repeat_rate": round(v3["repeat_rate"], 3),
                "v3_groundedness": v3["groundedness"],
                "v3_tool_groundedness": v3["tool_groundedness"],
                "v3_needs_evidence": v3["needs_evidence"],
                "v3_ids_in_db": v3["ids_in_db"],
                "v3_evidence_path_ok": v3["evidence_path_ok"],
                "v3_anchor_observed": v3["anchor_observed"],
            }
        )
    return {
        "mode": "scripted_fake",
        "dataset": str(dataset_path),
        "samples": len(ds.samples),
        "real_api": False,
        "recommendation": _RECOMMENDATION,
        "dataset_status": validation["status"],
        "dataset_errors": validation["errors"],
        "quality": {
            "precision": {
                "v2": round(_avg(v2_metrics, "precision"), 3),
                "v3": round(_avg(v3_metrics, "precision"), 3),
            },
            "recall": {
                "v2": round(_avg(v2_metrics, "recall"), 3),
                "v3": round(_avg(v3_metrics, "recall"), 3),
            },
            "f1": {"v2": round(_avg(v2_metrics, "f1"), 3), "v3": round(_avg(v3_metrics, "f1"), 3)},
            "position_accuracy": {
                "v2": round(_avg_position(v2_metrics), 3),
                "v3": round(_avg_position(v3_metrics), 3),
            },
            "negative_noise": {
                "v2": float(sum(m.negative_noise for m in v2_metrics)),
                "v3": float(sum(m.negative_noise for m in v3_metrics)),
            },
            "note": (
                "脚本化 Fake；V2 仅当 L3 prompt 含锚点才吐 Finding。"
                "带 tool_call_id 的 V3 候选只有 TOOL_RESULT，Pipeline 不验证该证据并打 0.8 折；"
                "脚本用 confidence=1.0（折后 0.8）才能过默认 min_confidence=0.75。"
                "若用与 V2 相同的 0.9，折后 0.72 会被抑制。"
                + (f"{' / '.join(excluded)} 不进本表。" if excluded else "")
                + "不代表真实模型质量。"
            ),
            "excluded": excluded,
        },
        "cost": {
            "model_calls": {
                "v2": int(sum(r["model_calls"] for r in v2_rows)),
                "v3": int(sum(r["model_calls"] for r in v3_rows)),
            },
            "input_tokens": {
                "v2": int(sum(r["input_tokens"] for r in v2_rows)),
                "v3": int(sum(r["input_tokens"] for r in v3_rows)),
            },
            "output_tokens": {
                "v2": int(sum(r["output_tokens"] for r in v2_rows)),
                "v3": int(sum(r["output_tokens"] for r in v3_rows)),
            },
            "total_tokens": {
                "v2": int(sum(r["input_tokens"] + r["output_tokens"] for r in v2_rows)),
                "v3": int(sum(r["input_tokens"] + r["output_tokens"] for r in v3_rows)),
            },
            "tool_calls": {
                "v2": int(sum(r["tool_calls"] for r in v2_rows)),
                "v3": int(sum(r["tool_calls"] for r in v3_rows)),
            },
            "cost_usd": {
                "v2": round(sum(r["cost_usd"] for r in v2_rows), 6),
                "v3": round(sum(r["cost_usd"] for r in v3_rows), 6),
            },
            "note": "Fake ModelUsage 累计（含 cost_usd），非真实 API 账单。",
        },
        "latency": {
            "repeats": repeats,
            "total_ms": {
                "v2": round(statistics.fmean(v2_walls), 2),
                "v3": round(statistics.fmean(v3_walls), 2),
            },
            "sum_sample_ms": {"v2": round(v2_ms, 2), "v3": round(v3_ms, 2)},
            "p50_ms": {
                "v2": round(_percentile(v2_walls, 50), 2),
                "v3": round(_percentile(v3_walls, 50), 2),
            },
            "p95_ms": {
                "v2": round(_percentile(v2_walls, 95), 2),
                "v3": round(_percentile(v3_walls, 95), 2),
            },
            "note": f"全数据集墙钟，repeats={repeats}；质量取第 1 次。Fake 延迟不代表真实模型。",
        },
        "agent_ops": {
            "tool_calls": int(sum(r["tool_calls"] for r in v3_rows)),
            "successful_tools": v3_success,
            "tool_attempts": v3_attempts,
            "effectiveness": round(
                tool_effectiveness(successful_tools=v3_success, tool_attempts=v3_attempts), 3
            ),
            "repeat_rate": round(
                repeat_rate(repeat_count=v3_repeats, tool_attempts=v3_attempts), 3
            ),
            "failure_rate": failure_rate(failed_or_partial=v3_failed, task_count=v3_tasks),
            "groundedness": None if not grounds else round(statistics.fmean(grounds), 3),
            "tool_groundedness": (
                None if not tool_grounds else round(statistics.fmean(tool_grounds), 3)
            ),
            "v2_tool_calls": int(sum(r["tool_calls"] for r in v2_rows)),
            "needs_evidence": int(sum(r["needs_evidence"] for r in v3_rows)),
            "note": (
                "控制工具不计 attempt。本表是数据集合计，不是 0.7/0.3 门槛；"
                "门槛只钉 xf-l3-miss / xf-spam-tools。"
                "groundedness 含 diff 行（Pipeline 已定位则常为 1.0）；"
                "tool_groundedness 只认 evidence.tool_call_id。"
                "V3 带 TOOL_RESULT 的 accepted 常 needs_evidence=True（Pipeline 不验证该证据）。"
            ),
        },
        "protocol_ab": {
            "native_completed": sum(1 for row in ab_rows if row["native_completed"]),
            "action_json_completed": sum(1 for row in ab_rows if row["action_json_completed"]),
            "samples": len(ab_rows),
            "all_passed": all(
                row["native_completed"] and row["action_json_completed"] for row in ab_rows
            ),
            "hits_preserved": all(row["native_hit"] == row["action_json_hit"] for row in ab_rows),
            "groundedness_preserved": all(
                row["native_tool_groundedness"] == row["action_json_tool_groundedness"]
                for row in ab_rows
            ),
            "cases": ab_rows,
            "note": (
                "action_json 臂经真实 Action JSON 解析器，命中样本 tool_call_id 为 aj-*；"
                "xf-l3-miss 另含 1 条 parse_error→repair。"
                "Fake 下两列接近是预期。协议可切换；选型等 live。不关闭 OQ-11。"
            ),
        },
        "per_sample": per_sample,
        "default_agent": default_agent,
        "v1_demo_default": v1_demo_default,
        "v3_file_tasks": _v3_settings("native").concurrency.file_tasks,
        "v3_file_tasks_is_default": (
            _v3_settings("native").concurrency.file_tasks == Settings().concurrency.file_tasks
        ),
        "v2_is_default_settings": _v2_settings().model_dump() == Settings().model_dump(),
        "user_version": default_agent["user_version"],
    }


def render_markdown(report: dict[str, Any]) -> str:
    q, c, lat = report["quality"], report["cost"], report["latency"]
    ops, ab = report["agent_ops"], report["protocol_ab"]
    default = report["default_agent"]
    demo = report["v1_demo_default"]
    fail = ops["failure_rate"]
    fail_s = "n/a" if fail is None else f"{fail:.3f}"
    ground = ops["groundedness"]
    ground_s = "n/a" if ground is None else f"{ground:.3f}"
    tool_ground = ops["tool_groundedness"]
    tool_ground_s = "n/a" if tool_ground is None else f"{tool_ground:.3f}"
    lines = [
        "# V3-D 跨文件 V2 vs V3 对照（脚本化 Fake，非真实 API）",
        "",
        "> 由 `python -m reposage.evals.v3d_compare` 生成；原始 JSON：`docs/evidence/v3-d-compare.json`。",
        f"> dataset=`{report['dataset']}` samples={report['samples']} "
        f"repeats={lat['repeats']} real_api={report['real_api']}",
        f"> recommendation=`{report['recommendation']}`",
        "> 脱敏：不含 API Key / 源码原文 / 密钥字面量。",
        "",
        "## 质量（Finding，macro）",
        "",
        "| 指标 | V2 single_pass | V3 agentic |",
        "|------|----------------|------------|",
        f"| Precision | {q['precision']['v2']} | {q['precision']['v3']} |",
        f"| Recall | {q['recall']['v2']} | {q['recall']['v3']} |",
        f"| F1 | {q['f1']['v2']} | {q['f1']['v3']} |",
        f"| 位置准确率 | {q['position_accuracy']['v2']} | {q['position_accuracy']['v3']} |",
        f"| 负样本噪声 | {q['negative_noise']['v2']} | {q['negative_noise']['v3']} |",
        "",
        q["note"],
        "",
        "## 成本（Fake 记账）",
        "",
        "| 指标 | V2 single_pass | V3 agentic |",
        "|------|----------------|------------|",
        f"| 模型调用数 | {c['model_calls']['v2']} | {c['model_calls']['v3']} |",
        f"| input tokens | {c['input_tokens']['v2']} | {c['input_tokens']['v3']} |",
        f"| output tokens | {c['output_tokens']['v2']} | {c['output_tokens']['v3']} |",
        f"| total tokens | {c['total_tokens']['v2']} | {c['total_tokens']['v3']} |",
        f"| 只读工具次数 | {c['tool_calls']['v2']} | {c['tool_calls']['v3']} |",
        f"| cost_usd | {c['cost_usd']['v2']} | {c['cost_usd']['v3']} |",
        "",
        c["note"],
        "",
        "## 延迟（全数据集墙钟）",
        "",
        "| 指标 | V2 single_pass | V3 agentic |",
        "|------|----------------|------------|",
        f"| total_ms | {lat['total_ms']['v2']} | {lat['total_ms']['v3']} |",
        f"| p50_ms | {lat['p50_ms']['v2']} | {lat['p50_ms']['v3']} |",
        f"| p95_ms | {lat['p95_ms']['v2']} | {lat['p95_ms']['v3']} |",
        "",
        lat["note"],
        "",
        "## Agent 运行",
        "",
        "| 指标 | V3 |",
        "|------|----|",
        f"| 只读工具次数 | {ops['tool_calls']} |",
        f"| successful_tools | {ops['successful_tools']} |",
        f"| tool_attempts | {ops['tool_attempts']} |",
        f"| 有效率 | {ops['effectiveness']} |",
        f"| 重复率 | {ops['repeat_rate']} |",
        f"| 失败/PARTIAL 率 | {fail_s} |",
        f"| groundedness | {ground_s} |",
        f"| tool_groundedness | {tool_ground_s} |",
        f"| needs_evidence（accepted） | {ops['needs_evidence']} |",
        f"| V2 工具次数 | {ops['v2_tool_calls']} |",
        "",
        ops["note"],
        "",
        "## 协议 A/B（native vs action_json）",
        "",
        f"native_completed={ab['native_completed']}/{ab['samples']} "
        f"action_json_completed={ab['action_json_completed']}/{ab['samples']} "
        f"all_passed={ab['all_passed']} hits_preserved={ab['hits_preserved']} "
        f"groundedness_preserved={ab['groundedness_preserved']}",
        "",
        "| id | native 完成 | action 完成 | native hit | action hit | native calls | action calls | action tool_call_ids | action repair |",
        "|----|-------------|-------------|------------|------------|--------------|--------------|----------------------|---------------|",
    ]
    for row in ab["cases"]:
        action_ids = ",".join(row["action_json_tool_call_ids"]) if row["action_json_tool_call_ids"] else ""
        lines.append(
            f"| {row['id']} | {row['native_completed']} | {row['action_json_completed']} "
            f"| {row['native_hit']} | {row['action_json_hit']} "
            f"| {row['native_calls']} | {row['action_json_calls']} "
            f"| {action_ids} | {row['action_json_json_repair']} |"
        )
    lines.extend(
        [
            "",
            ab["note"],
            "",
            "## 逐样本",
            "",
            "| id | l3_expected | V2 hit | V3 hit | V3 tool_call_ids | needs_evidence | ids_in_db | path_ok | anchor |",
            "|----|-------------|--------|--------|------------------|----------------|-----------|---------|--------|",
        ]
    )
    for row in report["per_sample"]:
        ids = ",".join(row["v3_tool_call_ids"]) if row["v3_tool_call_ids"] else ""
        lines.append(
            f"| {row['id']} | {row['l3_expected']} | {row['v2_hit']} | {row['v3_hit']} "
            f"| {ids} | {row['v3_needs_evidence']} | {row['v3_ids_in_db']} "
            f"| {row['v3_evidence_path_ok']} | {row['v3_anchor_observed']} |"
        )
    pins = {row["id"]: row for row in report["per_sample"]}
    lines.extend(
        [
            "",
            "## 脚本门槛（非 live SLA）",
            "",
            f"xf-l3-miss 有效率={pins['xf-l3-miss']['v3_effectiveness']} 重复率={pins['xf-l3-miss']['v3_repeat_rate']}（门槛 ≥0.7 / <0.3）",
            f"xf-spam-tools 有效率={pins['xf-spam-tools']['v3_effectiveness']}（须 <0.7）",
            f"xf-grounded tool_groundedness={pins['xf-grounded']['v3_tool_groundedness']} "
            f"tool_call_ids={','.join(pins['xf-grounded']['v3_tool_call_ids'])}",
            f"xf-ungrounded tool_call_ids 空、tool_groundedness={pins['xf-ungrounded']['v3_tool_groundedness']}（不进质量主表）",
            "xf-spam-tools 有效率分母样本，不进质量主表",
            "",
            "## 默认路径",
            "",
            f"V2 臂 = Settings() 默认（v2_is_default_settings={report['v2_is_default_settings']}）。",
            f"agent.enabled={default['enabled']} strategy={default['strategy']} "
            f"tool_protocol={default['tool_protocol']} default_run_tool_calls={default['tool_calls']}",
            f"V3 臂只覆盖 strategy=agentic + agent.enabled + tool_protocol；"
            f"file_tasks={report['v3_file_tasks']}（v3_file_tasks_is_default={report['v3_file_tasks_is_default']}）。",
            "",
            "## 附录：v1_demo 默认关闭",
            "",
            f"sample={demo['sample_id']} agent.enabled={demo['enabled']} "
            f"strategy={demo['strategy']} tool_calls={demo['tool_calls']}",
            "",
            f"real_api={report['real_api']} recommendation=`{report['recommendation']}` "
            f"user_version={report['user_version']} dataset_status={report['dataset_status']}",
            "",
            "真实 API 对照：未运行。OQ-11 live 仍后置。",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(
    report: dict[str, Any],
    *,
    json_path: str | Path = _DEFAULT_JSON,
    markdown_path: str | Path = _DEFAULT_MD,
) -> None:
    json_file = Path(json_path)
    md_file = Path(markdown_path)
    json_file.parent.mkdir(parents=True, exist_ok=True)
    json_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_file.write_text(render_markdown(report), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V2 vs V3 跨文件脚本化对照")
    parser.add_argument("--dataset", default=_DEFAULT_DATASET)
    parser.add_argument("--output", default=_DEFAULT_JSON)
    parser.add_argument("--markdown", default=_DEFAULT_MD)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)
    report = run_compare(args.dataset, repeats=args.repeats)
    write_report(report, json_path=args.output, markdown_path=args.markdown)
    print(
        f"wrote {args.output} and {args.markdown} "
        f"recommendation={report['recommendation']} real_api={report['real_api']} "
        f"dataset_status={report['dataset_status']}"
    )
    return 0 if report["dataset_status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
