"""工具执行信封：校验 → 沙箱 → 超时 → 截断（23 §3.3）。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from typing import Any

from pydantic import ValidationError

from reposage.domain.enums import ContextSourceKind, ToolCallStatus
from reposage.domain.models import ContextSource, ToolCall, ToolResult
from reposage.domain.protocols import Storage
from reposage.observability.logging import StructuredLogger, redact_secrets
from reposage.tools.registry import HandlerOutput, ToolRegistry, validation_error_code
from reposage.tools.sandbox import SandboxError
from reposage.tools.snapshot import ToolWorkspace

_LIST_KEYS = ("lines", "paths", "hits", "refs", "hunks")
_ERROR_MAX = 256


def new_tool_call_id() -> str:
    return f"tc-{uuid.uuid4().hex[:16]}"


def is_repeat(name: str, args: dict[str, Any], recent: list[ToolCall]) -> str | None:
    for prev in reversed(recent):
        if prev.name == name and prev.arguments == args:
            return prev.tool_call_id
    return None


def _source(name: str, sha: str, tool_call_id: str) -> ContextSource:
    return ContextSource(
        kind=ContextSourceKind.TOOL,
        ref=f"tool:{name}:{tool_call_id}",
        sha=sha,
    )


def _dump(payload: Any, source: ContextSource) -> str:
    envelope = {
        "payload": payload,
        "source": {"kind": source.kind.value, "ref": source.ref, "sha": source.sha},
    }
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True)


def encode_tool_data(
    payload: Any, source: ContextSource, *, max_chars: int
) -> tuple[str | None, bool]:
    """结构化信封；超限时从列表尾部丢弃，禁止 JSON 字符串中截断。"""
    text = _dump(payload, source)
    if len(text) <= max_chars:
        return text, False
    if isinstance(payload, dict):
        shrunk = dict(payload)
        for key in _LIST_KEYS:
            items = shrunk.get(key)
            if not isinstance(items, list):
                continue
            items = list(items)
            while items and len(_dump({**shrunk, key: items}, source)) > max_chars:
                items.pop()
            shrunk[key] = items
            shrunk["truncated"] = True
            text = _dump(shrunk, source)
            if len(text) <= max_chars:
                return text, True
        payload = shrunk
    if isinstance(payload, list):
        items = list(payload)
        while items and len(_dump(items, source)) > max_chars:
            items.pop()
        text = _dump(items, source)
        if len(text) <= max_chars:
            return text, True
    fallback = _dump({"truncated": True}, source)
    if len(fallback) <= max_chars:
        return fallback, True
    return None, True


def _clip_error(text: str) -> str:
    cleaned = redact_secrets(text).replace("\n", " ").strip()
    if len(cleaned) > _ERROR_MAX:
        return cleaned[:_ERROR_MAX]
    return cleaned


async def _persist(
    store: Storage | None,
    call: ToolCall,
    result: ToolResult,
    *,
    save_tool_trace: bool,
) -> None:
    if store is None or not save_tool_trace or not call.task_id:
        return
    await store.record_tool_invocation(call, result)


async def invoke(
    registry: ToolRegistry,
    workspace: ToolWorkspace,
    name: str,
    args: dict[str, Any] | None,
    *,
    tool_call_id: str | None = None,
    repeat_of: str | None = None,
    store: Storage | None = None,
    save_tool_trace: bool = True,
    logger: StructuredLogger | None = None,
) -> tuple[ToolCall, ToolResult]:
    call_id = tool_call_id or new_tool_call_id()
    raw_args = dict(args or {})
    started = time.perf_counter()
    call = ToolCall(
        tool_call_id=call_id,
        name=name,
        arguments=raw_args,
        task_id=workspace.task_id,
        repeat_of=repeat_of,
    )
    source = _source(name, workspace.snapshot.head_sha, call_id)
    result = ToolResult(tool_call_id=call_id, source=source)

    async def _finish(status: ToolCallStatus, error: str | None = None) -> tuple[ToolCall, ToolResult]:
        call.status = status
        if error:
            result.error = _clip_error(error)
        if logger is not None:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.log(
                event="tool_invoke",
                tool_call_id=call_id,
                task_id=workspace.task_id,
                run_id=workspace.run_id,
                detail=(
                    f"name={name} status={status.value} truncated={int(result.truncated)} "
                    f"duration_ms={duration_ms} repeat_of={call.repeat_of or '-'}"
                ),
            )
        await _persist(store, call, result, save_tool_trace=save_tool_trace)
        return call, result

    entry = registry.get(name)
    if entry is None:
        return await _finish(ToolCallStatus.INVALID_ARGS, "unknown_tool")

    try:
        parsed = registry.parse_args(name, raw_args)
    except ValidationError as exc:
        return await _finish(ToolCallStatus.INVALID_ARGS, validation_error_code(exc))

    async def _run() -> HandlerOutput:
        return await entry.handler(workspace, parsed)

    try:
        output = await asyncio.wait_for(_run(), timeout=entry.definition.timeout_s)
    except TimeoutError:
        return await _finish(ToolCallStatus.TIMEOUT, "timeout")
    except SandboxError as exc:
        return await _finish(ToolCallStatus.INVALID_ARGS, exc.code)
    except asyncio.CancelledError:
        call.status = ToolCallStatus.CANCELLED
        result.error = "cancelled"
        with contextlib.suppress(Exception):
            await _persist(store, call, result, save_tool_trace=save_tool_trace)
        raise
    except Exception as exc:
        return await _finish(ToolCallStatus.ERROR, type(exc).__name__)

    if output.status is not ToolCallStatus.OK or output.error:
        result.truncated = output.truncated
        return await _finish(output.status, output.error)

    data, extra_trunc = encode_tool_data(
        output.payload,
        source,
        max_chars=entry.definition.max_result_chars,
    )
    if data is None:
        return await _finish(ToolCallStatus.ERROR, "result_too_large")
    result.data = data
    result.truncated = output.truncated or extra_trunc
    parsed_env = json.loads(data)
    result.source = ContextSource(
        kind=ContextSourceKind(parsed_env["source"]["kind"]),
        ref=str(parsed_env["source"]["ref"]),
        sha=parsed_env["source"].get("sha"),
    )
    return await _finish(ToolCallStatus.OK)
