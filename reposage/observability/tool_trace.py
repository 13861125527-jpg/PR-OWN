"""工具审计字段规范化：脱敏 + 固定上限（V3-A P1-2）。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from reposage.observability.logging import redact_secrets

AUDIT_ARGS_MAX_CHARS = 8_000
AUDIT_DATA_MAX_CHARS = 32_000
AUDIT_ERROR_MAX_CHARS = 256
_LIST_KEYS = ("lines", "paths", "hits", "refs", "hunks")


def _dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _summary(kind: str, raw: str) -> str:
    return _dump(
        {
            "_audit_truncated": True,
            "_chars": len(raw),
            "_kind": kind,
            "_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        }
    )


def _shrink_obj(obj: Any, max_chars: int) -> str | None:
    text = _dump(obj)
    if len(text) <= max_chars:
        return text
    if isinstance(obj, dict):
        shrunk = dict(obj)
        for key in _LIST_KEYS:
            items = shrunk.get(key)
            if not isinstance(items, list):
                continue
            items = list(items)
            while items and len(_dump({**shrunk, key: items})) > max_chars:
                items.pop()
            shrunk[key] = items
            shrunk["truncated"] = True
            text = _dump(shrunk)
            if len(text) <= max_chars:
                return text
        payload = shrunk.get("payload")
        if isinstance(payload, dict):
            inner = _shrink_obj(payload, max(64, max_chars - 256))
            if inner is not None:
                shrunk["payload"] = json.loads(inner)
                shrunk["truncated"] = True
                text = _dump(shrunk)
                if len(text) <= max_chars:
                    return text
    if isinstance(obj, list):
        items = list(obj)
        while items and len(_dump(items)) > max_chars:
            items.pop()
        text = _dump(items)
        if len(text) <= max_chars:
            return text
    return None


def canonicalize_args_json(arguments: dict[str, Any]) -> tuple[str, bool]:
    raw = redact_secrets(_dump(arguments))
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        return _summary("args", raw), True
    if len(raw) <= AUDIT_ARGS_MAX_CHARS:
        return raw, False
    return _summary("args", raw), True


def canonicalize_data_json(data: str | None) -> tuple[str | None, bool]:
    if data is None:
        return None, False
    raw = redact_secrets(data)
    if len(raw) <= AUDIT_DATA_MAX_CHARS:
        return raw, False
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return _summary("data", raw), True
    fitted = _shrink_obj(obj, AUDIT_DATA_MAX_CHARS)
    if fitted is not None:
        return fitted, True
    return _summary("data", raw), True


def canonicalize_error(error: str | None) -> str | None:
    if error is None:
        return None
    cleaned = redact_secrets(error).replace("\n", " ").strip()
    return cleaned[:AUDIT_ERROR_MAX_CHARS]
